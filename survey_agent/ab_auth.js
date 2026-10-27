#!/usr/bin/env node
// TopSurveys authentication must be established through the normal login UI.
// Do not commit or inject account session cookies.
const { execSync } = require('child_process');
execSync('agent-browser open https://app.topsurveys.app/', { stdio: 'inherit', timeout: 15000 });
console.log('Complete normal sign-in in the browser before starting the survey agent.');
