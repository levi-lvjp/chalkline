import { jsx } from 'react/jsx-runtime';
import { ROUTES_AREA, SIDEBAR_NAV_AREA } from '@hermes/plugin-sdk';
import { OfficePage } from './page.js';

export default {
  id: 'chalkline',
  name: 'Chalkline',
  register(ctx) {
    ctx.registerMany([
      { id: 'office-page', area: ROUTES_AREA, data: { path: '/chalkline' },
        render: () => jsx(OfficePage, { ctx }) },
      { id: 'office-nav', area: SIDEBAR_NAV_AREA,
        data: { path: '/chalkline', label: 'Chalkline', codicon: 'briefcase' } },
    ]);
  },
};
