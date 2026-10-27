#!/usr/bin/env node
// agent-browser auth setup for TopSurveys
// Injects auth cookies via CDP so agent-browser can access the dashboard

const { execSync } = require('child_process');

const COOKIES = [
  { name: 'auth-token', value: '10943745%7CX1ARRCAR8zbFIhHt9VBAIKibSzFgqoWwRgAXUy4O44235d09' },
  { name: 'auth-provider', value: 'google' },
  { name: 'ps-uuid', value: 'a0763cb5-72e3-4099-a97c-84de8bccc4d4' },
  { name: 'ps-shash', value: '8b3c00397ae31b0993cfb58df347b235' },
  { name: 'ps-uhash', value: '9a48cc12-6a2d-4583-9c70-6dcfabae7576' },
  { name: 'ps-app', value: 'Y8wsEPawxK' },
  { name: 'ps-di', value: '397479eaf414ce0cb93d9ca747c05dc21853596066d6c01a390ec3dfa22e11a370052548f' },
];

function ab(cmd) {
  return execSync(`agent-browser ${cmd}`, { encoding: 'utf8', timeout: 15000 }).trim();
}

async function setup() {
  // Open topsurveys (needed to set cookies on the right domain)
  ab('open https://app.topsurveys.app/');
  
  // Inject cookies via eval
  const cookieScript = COOKIES.map(c =>
    `document.cookie = "${c.name}=${c.value}; path=/; SameSite=Lax";`
  ).join('\n');
  ab(`eval '${cookieScript} "done"'`);
  
  // Navigate to surveys
  ab('open https://app.topsurveys.app/surveys');
  
  console.log('Auth setup complete. Check with: agent-browser snapshot -i');
}

setup().catch(e => { console.error(e.message); process.exit(1); });
