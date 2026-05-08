const http = require('http');
const fs   = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const DATA = path.join(ROOT, 'data');
const PORT = 8080;
const TYPES = {
  '.html': 'text/html', '.css': 'text/css',
  '.js':   'application/javascript', '.json': 'application/json',
  '.png':  'image/png', '.ico': 'image/x-icon',
};

// ---------------------------------------------------------------------------
// Build data context at startup
// ---------------------------------------------------------------------------
const PLAYER_KEYS = [
  'name','team','division','dynamic_rating_baseline',
  'rating_30','rating_35','wl_record_30','wl_record_35',
  'lines_played_30','lines_played_35','notes_30','notes_35',
  'team_30','team_35',
];

const STRIP_KEYS = new Set([
  'scorecard_url','profile_url','tennisrecord_id','tennislink_id',
  'match_id','tl_match_id','status',
]);

function stripObj(obj) {
  if (Array.isArray(obj)) return obj.map(stripObj);
  if (obj && typeof obj === 'object') {
    const out = {};
    for (const [k, v] of Object.entries(obj)) {
      if (!STRIP_KEYS.has(k)) out[k] = stripObj(v);
    }
    return out;
  }
  return obj;
}

function loadContext() {
  const players = JSON.parse(fs.readFileSync(path.join(DATA, 'players.json'), 'utf8'));
  const slim = players.map(p => {
    const o = {};
    PLAYER_KEYS.forEach(k => { if (p[k] !== undefined) o[k] = p[k]; });
    return o;
  });
  const st35 = stripObj(JSON.parse(fs.readFileSync(path.join(DATA, 'standings_women_35.json'), 'utf8')));
  const st30 = stripObj(JSON.parse(fs.readFileSync(path.join(DATA, 'standings_women_30.json'), 'utf8')));
  const a35  = JSON.parse(fs.readFileSync(path.join(DATA, 'analysis_35.json'), 'utf8'));
  const a30  = JSON.parse(fs.readFileSync(path.join(DATA, 'analysis_30.json'), 'utf8'));
  return { players: slim, standings_35: st35, standings_30: st30, analysis_35: a35, analysis_30: a30 };
}

let CTX_JSON = '';
try {
  const ctx = loadContext();
  CTX_JSON = JSON.stringify(ctx);
  console.log(`[agent] Context: ${CTX_JSON.length.toLocaleString()} chars`);
} catch (e) {
  console.error('[agent] Context load failed:', e.message);
}

// ---------------------------------------------------------------------------
// System prompt  (loaded once, cached on every API call)
// ---------------------------------------------------------------------------
const SYSTEM = `You are a sharp tennis analytics assistant embedded in a USTA women's league dashboard for NV Area F 2026. You have access to live standings, player ratings, match results, and division analysis for the 3.0 and 3.5 Women divisions.

You can answer questions like:
- "Tell me about [player]" — give a player story: rating arc, notable wins/losses, trend
- "Who should [team] put at S1 / D1?" — lineup recommendations with rating context
- "What are [team]'s chances against [opponent]?" — matchup analysis
- "Who are the best singles players in 3.5?" — division-wide rankings
- "What does [team] need to clinch first place?" — standings math

Keep answers sharp and specific. Use actual ratings, records, and match details from the data. Avoid generic filler. When you mention a player, include their current rating.

Ratings use a sequential dynamic system — "baseline" is their NTRP-assigned start rating; "rating_35" / "rating_30" is their current adjusted rating. Higher is better within the division. A gap of 0.10 = ~58% win probability; 0.20 = ~68%; 0.30+ = ~75%.

Today's date: ${new Date().toLocaleDateString('en-US', {year:'numeric',month:'long',day:'numeric'})}.

Data (JSON):
${CTX_JSON}`;

// ---------------------------------------------------------------------------
// Chat handler
// ---------------------------------------------------------------------------
async function handleChat(req, res) {
  const apiKey = process.env.ANTHROPIC_API_KEY;
  if (!apiKey) {
    res.writeHead(500, { 'Content-Type': 'application/json' });
    return res.end(JSON.stringify({ error: 'ANTHROPIC_API_KEY environment variable is not set.' }));
  }
  if (!CTX_JSON) {
    res.writeHead(500, { 'Content-Type': 'application/json' });
    return res.end(JSON.stringify({ error: 'Context data failed to load on startup.' }));
  }

  let body = '';
  req.on('data', chunk => body += chunk);
  req.on('end', async () => {
    try {
      const { messages } = JSON.parse(body);
      const apiRes = await fetch('https://api.anthropic.com/v1/messages', {
        method: 'POST',
        headers: {
          'Content-Type':    'application/json',
          'x-api-key':       apiKey,
          'anthropic-version': '2023-06-01',
        },
        body: JSON.stringify({
          model:      'claude-opus-4-5',
          max_tokens: 1024,
          system: [
            {
              type: 'text',
              text: SYSTEM,
              cache_control: { type: 'ephemeral' },  // cache the large context
            }
          ],
          messages,
        }),
      });
      const data = await apiRes.json();
      if (data.error) throw new Error(data.error.message);
      const content = data.content?.[0]?.text ?? '(no response)';
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ content }));
    } catch (e) {
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: e.message }));
    }
  });
}

// ---------------------------------------------------------------------------
// HTTP server
// ---------------------------------------------------------------------------
http.createServer((req, res) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
  if (req.method === 'OPTIONS') { res.writeHead(204); return res.end(); }

  if (req.method === 'POST' && req.url === '/api/chat') return handleChat(req, res);

  // Static files
  if (req.url === '/') { res.writeHead(302, { Location: '/women_35.html' }); return res.end(); }
  let filePath = path.join(ROOT, req.url.split('?')[0]);
  if (!filePath.startsWith(ROOT)) { res.writeHead(403); return res.end(); }
  fs.readFile(filePath, (err, data) => {
    if (err) { res.writeHead(404); return res.end('Not found'); }
    res.writeHead(200, { 'Content-Type': TYPES[path.extname(filePath)] || 'text/plain' });
    res.end(data);
  });
}).listen(PORT, () => console.log(`Serving ${ROOT} on http://localhost:${PORT}`));
