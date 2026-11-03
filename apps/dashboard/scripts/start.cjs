process.env.HOSTNAME = process.env.DASHBOARD_HOST || '0.0.0.0';
require('../.next/standalone/server.js');
