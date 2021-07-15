"""Integration of native symbolication with Apple App Store Connect.

Sentry can download dSYMs directly from App Store Connect, this is the support code for
this.
"""

import dataclasses
import io
import logging
import pathlib
from datetime import datetime
from typing import Any, Dict, List

import dateutil
import jsonschema
import requests
from django.db import transaction

from sentry.lang.native.symbolicator import APP_STORE_CONNECT_SCHEMA
from sentry.models import Project
from sentry.utils import json
from sentry.utils.appleconnect import appstore_connect, itunes_connect

logger = logging.getLogger(__name__)


# The key in the project options under which all symbol sources are stored.
SYMBOL_SOURCES_PROP_NAME = "sentry:symbol_sources"


# The symbol source type for an App Store Connect symbol source.
SYMBOL_SOURCE_TYPE_NAME = "appStoreConnect"


class InvalidCredentialsError(Exception):
    """Invalid credentials for the App Store Connect API."""

    pass


class InvalidConfigError(Exception):
    """Invalid configuration for the appStoreConnect symbol source."""

    pass


class NoDsymsError(Exception):
    """No dSYMs were found."""

    pass


@dataclasses.dataclass(frozen=True)
class AppStoreConnectConfig:
    """The symbol source configuration for an App Store Connect source.

    This is stored as a symbol source inside symbolSources project option.
    """

    # The type of symbol source, can only be `appStoreConnect`.
    type: str

    # The ID which identifies this symbol source for this project.
    #
    # Currently we only allow one appStoreConnect source per project, but we already
    # identify them using an ID anyway for future safety.
    id: str

    # The name of the symbol source.
    #
    # Currently users can not chose this name, but it has a name anyway.
    name: str

    # Issuer ID for the API credentials.
    appconnectIssuer: str

    # Key ID for the API credentials.
    appconnectKey: str

    # Private key for the API credentials.
    appconnectPrivateKey: str

    # Username for the iTunes credentials.
    itunesUser: str

    # Password for the iTunes credentials.
    itunesPassword: str

    # Person ID of the iTunes user.
    #
    # This is an internal field that some iTunes calls need, but it is also relatively
    # easily to retrieve via an HTTP request to iTunes.
    itunesPersonId: str

    # The iTunes session cookie.
    #
    # Loading this cookie into ``requests.Session`` (see
    # ``sentry.utils.appleconnect.itunes_connect.load_session_cookie``) will allow this
    # session to make API iTunes requests as the user.
    itunesSession: str

    # The time the ``itunesSession`` cookie was created.
    #
    # The cookie only has a valid session for a limited time and needs user-interaction to
    # create it.  So we keep track of when it was created.
    itunesCreated: datetime

    # The name of the application, as supplied by the App Store Connect API.
    appName: str

    # The ID of the application in the App Store Connect API.
    #
    # We presume this is stable until proven otherwise.
    appId: str

    # The bundleID, e.g. io.sentry.sample.iOS-Swift.
    #
    # This is guaranteed to be unique and should map 1:1 to ``appId``.
    bundleId: str

    # The organisation ID according to iTunes.
    #
    # An iTunes session can have multiple organisations and needs this ID to be able to
    # select the correct organisation to operate on.
    orgId: int

    # The name of an organisation, as supplied by iTunes.
    orgName: str

    def __post_init__(self) -> None:
        # All fields are required.
        for field in dataclasses.fields(self):
            if not getattr(self, field.name, None):
                raise ValueError(f"Missing field: {field.name}")

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "AppStoreConnectConfig":
        """Creates a new instance from **deserialised** JSON data.

        This will include the JSON schema validation.  It accepts both a str or a datetime
        for the ``itunesCreated``.  Thus you can safely use this to create and validate the
        config as desrialised by both plain JSON deserialiser or by Django Rest Framework's
        deserialiser.

        :raises InvalidConfigError: if the data does not contain a valid App Store Connect
           symbol source configuration.
        """
        if isinstance(data["itunesCreated"], datetime):
            data["itunesCreated"] = data["itunesCreated"].isoformat()
        try:
            jsonschema.validate(data, APP_STORE_CONNECT_SCHEMA)
        except jsonschema.exceptions.ValidationError as e:
            raise InvalidConfigError from e
        data["itunesCreated"] = dateutil.parser.isoparse(data["itunesCreated"])
        return cls(**data)

    @classmethod
    def all_for_project(cls, project: Project) -> "List[AppStoreConnectConfig]":
        sources = []
        raw = project.get_option(SYMBOL_SOURCES_PROP_NAME, default="[]")
        all_sources = json.loads(raw)
        for source in all_sources:
            if source.get("type") == SYMBOL_SOURCE_TYPE_NAME:
                sources.append(cls.from_json(source))
        return sources

    @classmethod
    def from_project_config(cls, project: Project, config_id: str) -> "AppStoreConnectConfig":
        """Creates a new instance from the symbol source configured in the project.

        :raises KeyError: if the config is not found.
        :raises InvalidConfigError if the stored config is somehow invalid.
        """
        raw = project.get_option(SYMBOL_SOURCES_PROP_NAME, default="[]")
        all_sources = json.loads(raw)
        for source in all_sources:
            if source.get("type") == SYMBOL_SOURCE_TYPE_NAME and (source.get("id") == config_id):
                return cls.from_json(source)
        else:
            raise KeyError(f"No {SYMBOL_SOURCE_TYPE_NAME} symbol source found with id {config_id}")

    def to_json(self) -> Dict[str, Any]:
        """Creates a dict which can be serialised to JSON.

        The generated dict will validate according to the schema.

        :raises InvalidConfigError: if somehow the data in the class is not valid, this
           should only occur if the class was created in a weird way.
        """
        data = dict()
        for field in dataclasses.fields(self):
            value = getattr(self, field.name)
            if field.name == "itunesCreated":
                value = value.isoformat()
            data[field.name] = value
        try:
            jsonschema.validate(data, APP_STORE_CONNECT_SCHEMA)
        except jsonschema.exceptions.ValidationError as e:
            raise InvalidConfigError from e
        return data

    def update_project_symbol_source(self, project: Project) -> json.JSONData:
        """Updates this configuration in the Project's symbol sources.

        If a symbol source of type ``appStoreConnect`` already exists the ID must match and it
        will be updated.  If not ``appStoreConnect`` source exists yet it is added.

        :returns: The new value of the sources.  Use this in a call to
           `ProjectEndpoint.create_audit_entry()` to create an audit log.

        :raises ValueError: if an ``appStoreConnect`` source already exists but the ID does not
           match.
        """
        with transaction.atomic():
            all_sources_raw = project.get_option(SYMBOL_SOURCES_PROP_NAME, default="[]")
            all_sources = json.loads(all_sources_raw)
            for i, source in enumerate(all_sources):
                if source.get("type") == SYMBOL_SOURCE_TYPE_NAME:
                    if source.get("id") != self.id:
                        raise ValueError(
                            "Existing appStoreConnect symbolSource config does not match id"
                        )
                    all_sources[i] = self.to_json()
                    break
            else:
                # No existing appStoreConnect symbol source, simply append it.
                all_sources.append(self.to_json())
            project.update_option(SYMBOL_SOURCES_PROP_NAME, json.dumps(all_sources))
        return all_sources


@dataclasses.dataclass(frozen=True)
class BuildInfo:
    """Information about an App Store Connect build.

    A build is identified by the tuple of (app_id, platform, version, build_number), though
    Apple mostly names these differently.
    """

    # The app ID
    app_id: str

    # A platform identifying e.g. iOS, TvOS etc.
    #
    # These are not always human readable but some opaque string supplied by apple.
    platform: str

    # The human-readable version, e.g. "7.2.0".
    #
    # Each version can have multiple builds, Apple naming is a little confusing and calls
    # this "bundle_short_version".
    version: str

    # The build number, typically just a monotonically increasing number.
    #
    # Apple naming calls this the "bundle_version".
    build_number: str


class ITunesClient:
    """A client for the legacy iTunes API.

    Create this by calling :class:`AppConnectClient.itunes_client()`.

    On creation this will contact iTunes and will fail if it does not have a valid iTunes
    session.
    """

    def __init__(self, itunes_cookie: str, itunes_org: int):
        self._session = requests.Session()
        itunes_connect.load_session_cookie(self._session, itunes_cookie)
        # itunes_connect.set_provider(self._session, itunes_org)

    def download_dsyms(self, build: BuildInfo, path: pathlib.Path) -> None:
        url = itunes_connect.get_dsym_url(
            self._session, build.app_id, build.version, build.build_number, build.platform
        )
        if not url:
            raise NoDsymsError
        logger.debug("Fetching dSYM from: %s", url)
        with requests.get(url, stream=True) as req:
            req.raise_for_status()
            with open(path, "wb") as fp:
                for chunk in req.iter_content(chunk_size=io.DEFAULT_BUFFER_SIZE):
                    fp.write(chunk)


class AppConnectClient:
    """Client to interact with a single app from App Store Connect.

    Note that on creating this instance it will already connect to iTunes to set the
    provider for this session.  You also don't want to use the same iTunes cookie in
    multiple connections, so only make one client for a project.
    """

    def __init__(
        self,
        api_credentials: appstore_connect.AppConnectCredentials,
        itunes_cookie: str,
        itunes_org: int,
        app_id: str,
    ) -> None:
        """Internal init, use one of the classmethods instead."""
        self._api_credentials = api_credentials
        self._session = requests.Session()
        self._itunes_cookie = itunes_cookie
        self._itunes_org = itunes_org
        self._app_id = app_id

    @classmethod
    def from_project(cls, project: Project, config_id: str) -> "AppConnectClient":
        """Creates a new client for the project's appStoreConnect symbol source.

        This will load the configuration from the symbol sources for the project if a symbol
        source of the ``appStoreConnect`` type can be found which also has matching
        ``credentials_id``.
        """
        config = AppStoreConnectConfig.from_project_config(project, config_id)
        return cls.from_config(config)

    @classmethod
    def from_config(cls, config: AppStoreConnectConfig) -> "AppConnectClient":
        """Creates a new client from an appStoreConnect symbol source config.

        This config is normally stored as a symbol source of type ``appStoreConnect`` in a
        project's ``sentry:symbol_sources`` property.
        """
        api_credentials = appstore_connect.AppConnectCredentials(
            key_id=config.appconnectKey,
            key=config.appconnectPrivateKey,
            issuer_id=config.appconnectIssuer,
        )
        return cls(
            api_credentials=api_credentials,
            itunes_cookie=config.itunesSession,
            itunes_org=config.orgId,
            app_id=config.appId,
        )

    def itunes_client(self) -> ITunesClient:
        """Returns an iTunes client capable of downloading dSYMs.

        This will raise an exception if the session cookie is expired.
        """
        return ITunesClient(itunes_cookie=self._itunes_cookie, itunes_org=self._itunes_org)

    def list_builds(self) -> List[BuildInfo]:
        """Returns the available AppStore builds."""

        builds = []
        all_results = appstore_connect.get_build_info(
            self._session, self._api_credentials, self._app_id
        )
        for build in all_results:
            builds.append(
                BuildInfo(
                    app_id=self._app_id,
                    platform=build["platform"],
                    version=build["version"],
                    build_number=build["build_number"],
                )
            )

        return builds
