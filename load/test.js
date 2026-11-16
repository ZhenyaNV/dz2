import http from 'k6/http';
import exec from 'k6/execution';
import { Counter, Rate, Trend } from 'k6/metrics';

const base = __ENV.BASE_URL || 'http://app:8083';
const mode = __ENV.MODE || 'mixed';
const rate = Number(__ENV.RATE || 2);
const seconds = Number(__ENV.SECONDS || 30);
if (!['mixed', 'heavy'].includes(mode) || !Number.isInteger(rate) || rate < 1 || !Number.isInteger(seconds) || seconds < 1) {
  throw new Error('MODE=mixed|heavy, RATE and SECONDS must be positive integers');
}
// Every operation except login/logout. One iteration = one HTTP request.
export const weights = [['me', 3], ['services', 10], ['specialists', 10], ['list', 20],
  ['card', 15], ['slots', 25], ['create', 10], ['cancel', 5], ['summary', 2]];
const responseMs = new Trend('response_ms', true);
const errors = new Rate('request_errors');
const done = new Counter('requests_done');
const conflicts = new Counter('expected_conflicts');
const byOperation = new Counter('operation_count');
export const options = {
  scenarios: {
    [mode]: { executor: 'constant-arrival-rate', rate, timeUnit: '1s', duration: `${seconds}s`,
      preAllocatedVUs: Number(__ENV.VUS || Math.max(20, rate * 3)),
      maxVUs: Number(__ENV.MAX_VUS || Math.max(40, rate * 6)), gracefulStop: '30s' },
  },
  thresholds: { response_ms: ['p(95)<500'], request_errors: ['rate<0.01'] },
  summaryTrendStats: ['med', 'p(95)', 'max'],
};
export function setup() {
  const r = http.post(`${base}/api/login`, JSON.stringify({ username: 'demo', password: __ENV.DEMO_PASSWORD || 'demo12345' }),
    { headers: { 'Content-Type': 'application/json' }, tags: { operation: 'login' } });
  if (r.status !== 200 || !r.cookies.session) throw new Error(`Login failed: ${r.status}`);
  return { token: r.cookies.session[0].value };
}
function pickOperation() {
  const r = Math.random() * 100;
  let cumulative = 0;
  for (const [name, weight] of weights) { cumulative += weight; if (r < cumulative) return name; }
  return 'summary';
}
export default function(data) {
  const op = mode === 'heavy' ? 'summary' : pickOperation();
  const i = exec.scenario.iterationInTest;
  let path = '', method = 'GET', body = null;
  switch (op) {
    case 'me': case 'services': case 'specialists': case 'summary': path = `/api/${op}`; break;
    case 'list': path = '/api/appointments?page=1&size=20'; break;
    case 'card': path = `/api/appointments/${1 + i % 60000}`; break;
    case 'slots': path = '/api/slots?service_id=1&from_at=2026-10-01T00%3A00%3A00%2B03%3A00&to_at=2026-10-08T00%3A00%3A00%2B03%3A00&page=1&size=20'; break;
    // Seeded slots after 60000 are free. Service 1 is supported by every specialist.
    case 'create': path = '/api/appointments'; method = 'POST';
      body = JSON.stringify({slot_id: 60001 + i % 140000, service_id: 1, client_name: `evgenii_nevokshenov k6 ${i}`}); break;
    case 'cancel': path = `/api/appointments/${1 + i % 60000}/cancel`; method = 'POST'; break;
  }
  const expected = op === 'create' ? [201, 409] : op === 'cancel' ? [200, 409] : [200];
  const r = http.request(method, `${base}${path}`, body, {
    headers: { 'Content-Type': 'application/json' }, cookies: { session: data.token },
    tags: { operation: op, name: op }, timeout: '25s',
    responseCallback: http.expectedStatuses(...expected),
  });
  responseMs.add(r.timings.duration, { operation: op });
  const failed = !expected.includes(r.status);
  errors.add(failed, { operation: op });
  done.add(1); byOperation.add(1, { operation: op });
  if (r.status === 409 && expected.includes(409)) conflicts.add(1, { operation: op });
  if (failed) console.error(JSON.stringify({operation: op, iteration: i, status: r.status, body: r.body && r.body.slice(0, 800), error: r.error}));
}
export function handleSummary(data) {
  return { '/output/summary.json': JSON.stringify(data, null, 2) };
}
