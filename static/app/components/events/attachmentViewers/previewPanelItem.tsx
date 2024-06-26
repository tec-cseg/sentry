import styled from '@emotion/styled';

import PanelItem from 'sentry/components/panels/panelItem';

const PreviewPanelItem = styled(PanelItem)`
  overflow: auto;
  max-height: 500px;
  padding: 0;
`;

export default PreviewPanelItem;
