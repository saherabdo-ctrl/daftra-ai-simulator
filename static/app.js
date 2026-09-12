import { Room, RoomEvent } from 'livekit-client';

const TOKEN_KEY = 'sdr_token';
const CANDIDATE_TOKEN_KEY = 'candidate_token';
const ROLE_LABELS = { sdr: 'مندوب مبيعات', quality: 'فريق الجودة', admin: 'مدير' };

function getToken() {
  return localStorage.getItem(TOKEN_KEY) || localStorage.getItem(CANDIDATE_TOKEN_KEY) || '';
}
function setToken(t) {
  if (t) localStorage.setItem(TOKEN_KEY, t);
  else localStorage.removeItem(TOKEN_KEY);
}

const api = {
  async request(path, options = {}) {
    const headers = options.headers || {};
    const token = getToken();
    if (token) headers['Authorization'] = `Bearer ${token}`;
    const res = await fetch(path, { ...options, headers });
    if (res.status === 401) {
      const candidateToken = localStorage.getItem(CANDIDATE_TOKEN_KEY);
      setToken('');
      setCandidateToken('');
      if (candidateToken) {
        renderCandidateLogin('Session expired — please login again');
      } else {
        renderLogin('انتهت الجلسة — سجّل الدخول مجددًا');
      }
      throw new Error('Unauthorized');
    }
    if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
    return res.json();
  },
  get: (path) => api.request(path),
  post: (path, body) =>
    api.request(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  del: (path) => api.request(path, { method: 'DELETE' }),
  upload: (path, file) => {
    const fd = new FormData();
    fd.append('file', file);
    return api.request(path, { method: 'POST', body: fd });
  },
  login: (username, password) => api.post('/api/login', { username, password }),
  logout: () => api.post('/api/logout', {}),
  me: () => api.get('/api/me'),
  getScenarios: () => api.get('/api/scenarios'),
  getEvaluation: () => api.get('/api/evaluation'),
  getToken: (scenario) => api.post('/api/token', { scenario }),
  getResults: (room) => api.get(`/api/results/${encodeURIComponent(room)}`),
  createCustom: (brief) => api.post('/api/scenarios/custom', brief),
  uploadCall: (file) => api.upload('/api/upload-call', file),
  randomCall: () => api.post('/api/random-call', {}),
  // Candidate endpoints
  candidateLogin: (email, candidate_id) =>
    api.post('/api/candidate/login', { email, candidate_id }),
  candidateStartCall: () => api.post('/api/candidate/start-call', {}),
  candidateStatus: () => api.get('/api/candidate/status'),
};

const app = document.getElementById('app');

let user = null;
let room = null;
let scenarios = [];
let categories = [];
let users = [];
let bulkResult = [];
let current = null; // { scenario, credentials, startedAt, timer }
let agentJoined = false;
let audioSubscribed = false;
let audioCtx = null;
let meterTimer = null;

function escapeHtml(value) {
  return String(value == null ? '' : value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');
}

function formatDuration(seconds) {
  const m = String(Math.floor(seconds / 60)).padStart(2, '0');
  const s = String(seconds % 60).padStart(2, '0');
  return `${m}:${s}`;
}

function render(html) {
  app.innerHTML = html;
  return app;
}

/* ---------------- تسجيل الدخول ---------------- */

function renderLogin(error = '') {
  render(`
    <header class="topbar">
      <div class="topbar-inner">
        <span class="logo">🎙️</span>
        <h1>AI Simulator</h1>
      </div>
    </header>
    <main class="container">
      <div class="card login-card">
        <h2>Internal User Login</h2>
        <p class="muted">Enter your email and access code from the Heads sheet.</p>
        ${error ? `<div class="alert">${escapeHtml(error)}</div>` : ''}
        <form id="login-form" class="form-grid">
          <label>Email
            <input name="username" type="email" autocomplete="email" required placeholder="e.g. user@company.com" />
          </label>
          <label>Access Code
            <input name="password" type="password" autocomplete="off" required placeholder="e.g. HF-XXXXXXXX" />
          </label>
          <button type="submit" class="btn btn-primary full">Login</button>
        </form>
        <p class="muted small" style="margin-top: 1rem;">
          <a href="#" id="switch-to-candidate">Login as candidate</a>
        </p>
      </div>
    </main>`);

  document.getElementById('login-form').addEventListener('submit', (e) => {
    e.preventDefault();
    doLogin();
  });

  document.getElementById('switch-to-candidate').addEventListener('click', (e) => {
    e.preventDefault();
    renderCandidateLogin();
  });
}

async function doLogin() {
  const form = document.getElementById('login-form');
  const username = form.elements.username.value.trim();
  const password = form.elements.password.value;
  try {
    const res = await api.post('/api/login', { username, password });
    setToken(res.token);
    user = res.user;
    await loadRoleData();
    renderLanding();
  } catch (err) {
    let msg = err.message || 'Unknown error';
    if (msg.includes('401')) msg = 'Invalid credentials or inactive account';
    renderLogin(`Login failed: ${msg}`);
  }
}

function logout() {
  api.logout().catch(() => {});
  setToken('');
  user = null;
  scenarios = [];
  users = [];
  renderLogin();
}

async function loadRoleData() {
  categories = (await api.getEvaluation()).categories || [];
  if (user.role === 'sdr') {
    scenarios = [];
  } else {
    const sc = await api.getScenarios();
    scenarios = sc.scenarios || [];
  }
  if (user.role === 'admin') {
    try {
      users = (await api.getUsers()).users || [];
    } catch (_) {
      users = [];
    }
  }
}

/* ---------------- الرئيسية (حسب الدور) ---------------- */

function scenarioCard(s) {
  const cold = s.temperature === 'cold';
  const personaTag = s.persona_label
    ? `<span class="badge-persona">🧑‍💼 ${escapeHtml(s.persona_label)}</span>`
    : '';
  const dialectTag = s.dialect_label
    ? `<span class="badge-persona badge-dialect">🗣️ لهجة ${escapeHtml(s.dialect_label)}</span>`
    : '';
  return `
    <div class="card scenario-card" data-id="${escapeHtml(s.id)}">
      <div class="scenario-badge ${cold ? 'cold' : ''}">${cold ? 'عميل لا يعرفنا' : 'عميل يعرف دفترة'}</div>
      <h3>${escapeHtml(s.name)}</h3>
      <p class="muted">${escapeHtml(s.customer_role)}</p>
      <p>${escapeHtml(s.summary)}</p>
      <p class="muted small">~${escapeHtml(s.duration_hint)} ${personaTag} ${dialectTag}</p>
      <button class="btn btn-primary" data-action="start" data-id="${escapeHtml(s.id)}">
        🎙️ ابدأ التمرين
      </button>
    </div>`;
}

function roleBadge() {
  const role = user ? user.role : 'sdr';
  return `<span class="role-badge">${ROLE_LABELS[role] || role}</span>`;
}

function topbar(userName) {
  return `
    <header class="topbar">
      <div class="topbar-inner">
        <span class="logo">🎙️</span>
        <h1>معمل تدريب المبيعات</h1>
        <div class="topbar-user">
          <span class="muted small">${escapeHtml(userName || '')}</span>
          ${roleBadge()}
          <button class="btn-logout" id="btn-logout">خروج</button>
        </div>
      </div>
    </header>`;
}

function renderLanding(error = '', formError = '', uploading = false, uploadError = '', usersMsg = '') {
  if (!user) {
    renderLogin();
    return;
  }
  const isAdmin = user.role === 'admin';
  const isSdr = user.role === 'sdr';
  const scenarioCards = scenarios.map(scenarioCard).join('');

  const sdrSection = isSdr
    ? `
    <section class="hero">
      <h2>مرحبًا ${escapeHtml(user.name || user.username)} 👋</h2>
      <p class="muted">
        اضغط على الزر وسيتم اختيار عميل عشوائي لك. قدّم نفسك وشركة دفترة أولًا،
        ثم اكتشف احتياجه وتدرّب بشكل طبيعي.
      </p>
      ${error ? `<div class="alert">${escapeHtml(error)}</div>` : ''}
    </section>
    <section class="card random-card">
      <p class="muted">جلسة تدريب جديدة — عميل مختلف في كل مرة</p>
      <button class="btn btn-primary btn-big" id="btn-random">🎲 مكالمة عشوائية</button>
    </section>`
    : '';

  const clientsSection = !isSdr
    ? `
    <section>
      <div class="section-actions">
        <h2 class="section-title">العملاء</h2>
        <button class="btn btn-ghost btn-inline" id="btn-random">🎲 مكالمة عشوائية</button>
      </div>
      ${error ? `<div class="alert">${escapeHtml(error)}</div>` : ''}
      <div class="cards">${scenarioCards || '<p class="muted">لا يوجد عملاء بعد.</p>'}</div>
    </section>`
    : '';

  const createSection = isAdmin
    ? `
    <section class="card custom-card">
      <h2 class="section-title">➕ أنشئ عميلًا جديدًا</h2>
      <p class="muted">عرّف شخصية العميل (الاسم، مجال العمل، نقطة الألم، مستوى الصعوبة). أضف وصف منتجك إن أردت — سيصبح العميل الذكي متمكّنًا منه.</p>
      ${formError ? `<div class="alert">${escapeHtml(formError)}</div>` : ''}
      <form id="custom-form" class="form-grid">
        <label>اسم السيناريو (اختياري)
          <input name="name" placeholder="مثال: عميل جديد – برنامج المحاسبة" />
        </label>
        <label>اسم العميل *
          <input name="customer_name" placeholder="مثال: خالد العمري" required />
        </label>
        <label>منصب العميل
          <input name="customer_role" placeholder="مثال: مالك شركة / مدير مشتريات" />
        </label>
        <label>اسم الشركة
          <input name="company_name" placeholder="مثال: شركة الأفق للتجارة" />
        </label>
        <label>مجال العمل *
          <input name="business_field" placeholder="مثال: توزيع بالجملة / مستلزمات طبية" required />
        </label>
        <label>حجم الشركة
          <input name="company_size" placeholder="مثال: 50 موظفًا" />
        </label>
        <label>نقطة الألم / المشكلة الأساسية
          <input name="pain_point" placeholder="مثال: إدارة المخزون بجداول إكسل يدويًا" />
        </label>
        <label>صاحب القرار
          <select name="decision_maker">
            <option value="true">نعم، أنا من يقرر</option>
            <option value="false">لا، لست صاحب القرار</option>
          </select>
        </label>
        <label>هل يعرف العميل شركة دفترة؟
          <select name="temperature">
            <option value="warm" selected>نعم، يعرفنا (الأغلبية)</option>
            <option value="cold">لا، ما يعرفنا (مكالمة باردة)</option>
          </select>
        </label>
        <label>الحساسية للميزانية
          <select name="budget_sensitivity">
            <option value="منخفضة">منخفضة</option>
            <option value="متوسطة" selected>متوسطة</option>
            <option value="عالية">عالية</option>
          </select>
        </label>
        <label>نية الشراء
          <select name="buying_intent">
            <option value="منخفضة">منخفضة</option>
            <option value="متوسطة" selected>متوسطة</option>
            <option value="عالية">عالية</option>
          </select>
        </label>
        <label>مستوى الصعوبة
          <select name="difficulty">
            <option value="easy">سهل</option>
            <option value="medium" selected>متوسط</option>
            <option value="hard">صعب</option>
          </select>
        </label>
        <label>شخصية العميل
          <select name="persona">
            <option value="" selected>بدون تخصيص (حسب الصعوبة)</option>
            <option value="friendly">ودود</option>
            <option value="busy">مشغول</option>
            <option value="hesitant">متردد</option>
            <option value="price_sensitive">حساس للسعر</option>
            <option value="skeptical">متشكك</option>
            <option value="angry">مزعوج</option>
            <option value="confused">محتار</option>
            <option value="interested">متحمس</option>
            <option value="low_intent">غير مهتم</option>
            <option value="competitor">مستخدم منافس</option>
            <option value="difficult">صعب</option>
            <option value="indecisive">ما يعرف يقرر</option>
          </select>
        </label>
        <label>لهجة العميل
          <select name="dialect">
            <option value="saudi" selected>سعودية</option>
            <option value="egyptian">مصرية</option>
          </select>
        </label>
        <label>معرفة العميل بالمحاسبة
          <select name="knowledgeable">
            <option value="true" selected>محاسب/فاهم في المحاسبة</option>
            <option value="false">عميل عادي</option>
          </select>
        </label>
        <label class="full">العمر التقريبي (اختياري)
          <input name="approximate_age" placeholder="مثال: 35–40" />
        </label>
        <label class="full">وصف المنتج أو الخدمة (اختياري — سيتقنه العميل)
          <textarea name="product_brief" rows="4" placeholder="مثال: منصة سحابية لإدارة المخزون والمحاسبة، ميزاتها كذا…، التسعير يبدأ من …"></textarea>
        </label>
        <button type="submit" class="btn btn-secondary full">➕ إنشاء العميل والبدء</button>
      </form>
    </section>`
    : '';

  const uploadSection = isAdmin
    ? `
    <section class="card custom-card">
      <h2 class="section-title">🎧 علّمنا من مكالماتك الحقيقية</h2>
      <p class="muted">
        ارفع مكالمة مسجلة (MP3) مع عميل حقيقي وسنتعلم لهجته وبيئته، وننشئ منه
        عميلًا ذكيًا جديدًا ونسهّل من مكالماته على كل السيناريوهات.
      </p>
      ${uploadError ? `<div class="alert">${escapeHtml(uploadError)}</div>` : ''}
      ${uploading
        ? `<div class="alert">جارٍ تحليل المكالمة… قد يستغرق 30–60 ثانية. لا تغلق الصفحة.</div>`
        : `
        <form id="upload-form" class="form-grid">
          <label class="full">ملف المكالمة
            <input name="file" type="file"
              accept=".mp3,.m4a,.wav,.webm,.ogg,audio/mpeg,audio/mp3,audio/x-m4a,audio/wav,audio/webm,audio/ogg"
              required />
          </label>
          <button type="submit" class="btn btn-secondary full">🎧 تحليل المكالمة وإنشاء عميل جديد</button>
        </form>`}
    </section>`
    : '';

  const usersSection = isAdmin
    ? `
    <section class="card custom-card">
      <h2 class="section-title">👥 المستخدمون والأدوار</h2>
      <p class="muted">أدوار: مندوب مبيعات (مكالمة عشوائية فقط) · فريق الجودة (اختيار العملاء) · مدير (كل الصلاحيات).</p>
      ${usersMsg ? `<div class="alert">${escapeHtml(usersMsg)}</div>` : ''}
      <div class="users-list">
        ${users
          .map(
            (u) => `
          <div class="user-row">
            <span class="user-name">${escapeHtml(u.name || u.username)}</span>
            <span class="muted small">@${escapeHtml(u.username)}</span>
            <span class="user-role">${ROLE_LABELS[u.role] || u.role}</span>
            <button class="btn-danger-ghost" data-action="del-user" data-username="${escapeHtml(u.username)}" ${u.username === 'admin' ? 'disabled title="لا يمكن حذف المدير"' : ''}>حذف</button>
          </div>`
          )
          .join('')}
      </div>
      <form id="users-form" class="form-grid">
        <label>اسم المستخدم
          <input name="username" placeholder="مثال: m.alqahtani" required />
        </label>
        <label>الاسم الظاهر
          <input name="name" placeholder="مثال: محمد القحطاني" />
        </label>
        <label>الدور
          <select name="role">
            <option value="sdr">مندوب مبيعات</option>
            <option value="quality">فريق الجودة</option>
            <option value="admin">مدير</option>
          </select>
        </label>
        <label>كلمة المرور
          <input name="password" type="password" required />
        </label>
        <button type="submit" class="btn btn-secondary full">➕ إضافة / تحديث مستخدم</button>
      </form>

      <hr class="divider" />
      <h3 class="section-title">توليد حسابات جماعي</h3>
      <p class="muted">اكتب الأسماء (كل اسم في سطر) وسننشئ لكل اسم حسابًا باسم مستخدم تلقائي وكلمة مرور عشوائية — تظهر هنا مرة واحدة فقط لتنقلها لمن يخصّها.</p>
      <form id="bulk-form" class="form-grid">
        <label>بادئة اسم المستخدم
          <input name="base_username" value="sdr" placeholder="مثال: sdr" required />
        </label>
        <label>الدور
          <select name="role">
            <option value="sdr">مندوب مبيعات</option>
            <option value="quality">فريق الجودة</option>
            <option value="admin">مدير</option>
          </select>
        </label>
        <label class="span2">الأسماء (كل اسم في سطر)
          <textarea name="names" rows="6" placeholder="محمد القحطاني&#10;سارة العتيبي&#10;خالد الشمري" required></textarea>
        </label>
        <button type="submit" class="btn btn-secondary full">⚡ توليد الحسابات</button>
      </form>
      ${bulkResult && bulkResult.length
        ? `
      <table class="bulk-table">
        <thead>
          <tr><th>الاسم</th><th>اسم المستخدم</th><th>كلمة المرور</th><th></th></tr>
        </thead>
        <tbody>
          ${bulkResult
            .map(
              (c) => `
            <tr>
              <td>${escapeHtml(c.name)}</td>
              <td dir="ltr">${escapeHtml(c.username)}</td>
              <td dir="ltr" class="mono">${escapeHtml(c.password)}</td>
              <td><button class="btn btn-ghost btn-inline" data-action="copy-creds" data-creds="${escapeHtml(c.username + ' / ' + c.password)}">نسخ</button></td>
            </tr>`
            )
            .join('')}
        </tbody>
      </table>
      <button class="btn btn-ghost btn-inline" id="btn-copy-all">📋 نسخ الكل</button>`
        : ''}
    </section>`
    : '';

  render(
    topbar(user.name || user.username) +
    `
    <main class="container">
      ${sdrSection}
      ${clientsSection}
      ${createSection}
      ${uploadSection}
      ${usersSection}
      ${isSdr ? `
      <section class="callout">
        <h3>كيف يعمل</h3>
        <ol>
          <li>اضغط <strong>مكالمة عشوائية</strong> وسيتم اختيار عميل لك.</li>
          <li>اسمح بالوصول إلى الميكروفون عندما يطلب المتصفح ذلك.</li>
          <li>قدّم نفسك وشركة دفترة أولًا، ثم اكتشف احتياج العميل.</li>
          <li>اضغط <strong>إنهاء المكالمة</strong> لتحصل فورًا على بطاقة التقييم.</li>
        </ol>
      </section>` : ''}
    </main>`
  );

  document.getElementById('btn-logout').addEventListener('click', logout);
  const rb = document.getElementById('btn-random');
  if (rb) rb.addEventListener('click', startRandomCall);

  const startBtns = app.querySelectorAll('[data-action="start"]');
  startBtns.forEach((btn) => btn.addEventListener('click', () => startCall(btn.dataset.id)));

  const customForm = document.getElementById('custom-form');
  if (customForm) {
    customForm.addEventListener('submit', (e) => {
      e.preventDefault();
      submitCustomScenario();
    });
  }

  const uploadForm = document.getElementById('upload-form');
  if (uploadForm) {
    uploadForm.addEventListener('submit', (e) => {
      e.preventDefault();
      submitCallUpload();
    });
  }

  const usersForm = document.getElementById('users-form');
  if (usersForm) {
    usersForm.addEventListener('submit', (e) => {
      e.preventDefault();
      submitUser();
    });
  }
  const bulkForm = document.getElementById('bulk-form');
  if (bulkForm) {
    bulkForm.addEventListener('submit', (e) => {
      e.preventDefault();
      submitBulkUsers();
    });
  }
  const copyAllBtn = document.getElementById('btn-copy-all');
  if (copyAllBtn) {
    copyAllBtn.addEventListener('click', () => {
      const text = bulkResult.map((c) => `${c.name} | ${c.username} | ${c.password}`).join('\n');
      copyText(text);
    });
  }
  app.querySelectorAll('[data-action="copy-creds"]').forEach((btn) => {
    btn.addEventListener('click', () => copyText(btn.dataset.creds));
  });
  app.querySelectorAll('[data-action="del-user"]').forEach((btn) => {
    btn.addEventListener('click', () => deleteUser(btn.dataset.username));
  });
}

function copyText(text) {
  navigator.clipboard
    ?.writeText(text)
    .then(() => renderLanding('', '', false, '', 'تم النسخ ✅'))
    .catch(() => renderLanding('', '', false, '', 'تعذّر النسخ — انسخ يدويًا'));
}

function collectForm(form) {
  const data = {};
  for (const el of form.elements) {
    if (el.name) data[el.name] = el.value;
  }
  data.decision_maker = data.decision_maker === 'true';
  data.knowledgeable = data.knowledgeable === 'true';
  return data;
}

async function submitCustomScenario() {
  const form = document.getElementById('custom-form');
  const brief = collectForm(form);
  try {
    const res = await api.createCustom(brief);
    const scenario = res.scenario;
    scenarios.push(scenario);
    renderLanding();
    startCall(scenario.id);
  } catch (err) {
    renderLanding('', `تعذّر إنشاء السيناريو: ${err.message}`);
  }
}

function submitCallUpload() {
  const form = document.getElementById('upload-form');
  const file = form.elements.file.files[0];
  if (!file) return;
  renderLanding('', '', true);
  api
    .uploadCall(file)
    .then(async (res) => {
      const sc = await api.getScenarios();
      scenarios = sc.scenarios || [];
      const name = res.scenario?.customer_name || 'العميل الجديد';
      renderLanding('', '', false, `تم إنشاء عميل جديد: «${name}» مع ${res.learned_phrases || 0} عبارات عامية جديدة مضافة للأساس.`);
    })
    .catch((err) => {
      renderLanding('', '', false, `تعذّر تحليل المكالمة: ${err.message}`);
    });
}

async function startRandomCall() {
  try {
    const res = await api.randomCall();
    const scenario = res.scenario;
    if (!scenarios.some((s) => s.id === scenario.id)) scenarios.push(scenario);
    startCall(scenario.id);
  } catch (err) {
    renderLanding(`تعذّر اختيار عميل: ${err.message}`);
  }
}

async function submitUser() {
  const form = document.getElementById('users-form');
  const data = {
    username: form.elements.username.value.trim(),
    name: form.elements.name.value.trim(),
    role: form.elements.role.value,
    password: form.elements.password.value,
  };
  try {
    await api.createUser(data);
    users = (await api.getUsers()).users || [];
    renderLanding('', '', false, '', 'تم حفظ المستخدم');
  } catch (err) {
    renderLanding('', '', false, '', `تعذّر حفظ المستخدم: ${err.message}`);
  }
}

async function deleteUser(username) {
  try {
    await api.deleteUser(username);
    users = (await api.getUsers()).users || [];
    renderLanding('', '', false, '', `تم حذف المستخدم ${username}`);
  } catch (err) {
    renderLanding('', '', false, '', `تعذّر حذف المستخدم: ${err.message}`);
  }
}

async function submitBulkUsers() {
  const form = document.getElementById('bulk-form');
  const names = (form.elements.names.value || '')
    .split('\n')
    .map((s) => s.trim())
    .filter(Boolean);
  const base = form.elements.base_username.value.trim();
  const role = form.elements.role.value;
  try {
    const res = await api.bulkUsers(names, base, role);
    bulkResult = res.users || [];
    users = (await api.getUsers()).users || [];
    form.reset();
    renderLanding(
      '',
      '',
      false,
      '',
      `تم إنشاء ${bulkResult.length} حساب — انسخ الاعتمادات بالأسفل وسلّمها لأصحابها.`
    );
  } catch (err) {
    renderLanding('', '', false, '', `تعذّر توليد الحسابات: ${err.message}`);
  }
}

/* ---------------- المكالمة ---------------- */

async function startCall(scenarioId) {
  try {
    const credentials = await api.getToken(scenarioId);
    const scenario = scenarios.find((s) => s.id === scenarioId);
    if (!scenario) throw new Error('السيناريو غير موجود');
    await connectCall(scenario, credentials);
  } catch (err) {
    renderLanding(`تعذّر بدء المكالمة: ${err.message}`);
  }
}

async function connectCall(scenario, credentials) {
  renderCall(scenario, 'جارٍ الاتصال…');

  agentJoined = false;
  audioSubscribed = false;
  room = new Room();
  room.on(RoomEvent.ParticipantConnected, () => {
    agentJoined = true;
    updateCallStatus();
  });
  room.on(RoomEvent.TrackSubscribed, (track) => {
    if (track.kind === 'audio') {
      audioSubscribed = true;
      forcePlayTrack(track);
      attachMeter(track);
      room.startAudio().catch(() => {});
    }
    updateCallStatus();
  });
  room.on(RoomEvent.TrackMuted, updateMicUI);
  room.on(RoomEvent.TrackUnmuted, updateMicUI);
  room.on(RoomEvent.Disconnected, onDisconnected);
  // استقبال رسالة رنين التليفون من الـagent
  room.on(RoomEvent.DataReceived, (payload, participant, kind, topic) => {
    try {
      const text = typeof payload === 'string' ? payload : new TextDecoder().decode(payload);
      if (text && text.includes('"ring"')) {
        const ringAudio = new Audio('/static/ring.wav');
        ringAudio.volume = 0.8;
        ringAudio.play().catch(() => {});
      }
    } catch (_) {}
  });

  try {
    await room.connect(credentials.url, credentials.token);
    await room.localParticipant.setMicrophoneEnabled(true);
    if (room.remoteParticipants.size > 0) agentJoined = true;
    try {
      await room.startAudio();
    } catch (_) {
      /* audio may still autoplay */
    }
    current = { scenario, credentials, startedAt: Date.now(), timer: null };
    startTimer();
    updateCallStatus();
    updateMicUI();
    setTimeout(() => {
      if (!agentJoined && room) {
        setStatus(
          'لم يردّ العميل بعد. تأكد من تشغيل «python agent.py dev» ثم أنهِ المكالمة وحاول مجددًا.'
        );
      }
    }, 20000);
  } catch (err) {
    try {
      room.disconnect();
    } catch (_) {
      /* تجاهل */
    }
    renderLanding(`تعذّر الاتصال: ${err.message}`);
  }
}

function updateCallStatus() {
  if (!room) return;
  if (audioSubscribed) {
    setStatus('المكالمة جارية — استمع وتحدّث إلى عميلك');
  } else if (agentJoined) {
    setStatus('المكالمة جارية — تحدّث إلى عميلك');
  } else {
    setStatus('جارٍ توصيلك بعميلك…');
  }
}

function forcePlayTrack(track) {
  try {
    if (track.attachedElements && track.attachedElements.length) {
      track.attachedElements.forEach((el) => {
        el.muted = false;
        el.volume = 1;
        el.play().catch(() => {});
      });
    } else {
      track.attach().then((el) => {
        el.muted = false;
        el.volume = 1;
        el.play().catch(() => {});
      });
    }
  } catch (_) {
    /* تجاهل */
  }
}

function attachMeter(track) {
  const el = document.getElementById('audio-meter');
  if (!el) return;
  try {
    if (!audioCtx) {
      const AC = window.AudioContext || window.webkitAudioContext;
      audioCtx = new AC();
    }
    if (audioCtx.state === 'suspended') audioCtx.resume();
    let ms = null;
    if (track.mediaStreamTrack) ms = new MediaStream([track.mediaStreamTrack]);
    else if (track.attachedElements && track.attachedElements.length) {
      ms = track.attachedElements[0].srcObject;
    }
    if (!ms) return;
    const src = audioCtx.createMediaStreamSource(ms);
    const analyser = audioCtx.createAnalyser();
    analyser.fftSize = 256;
    src.connect(analyser);
    const data = new Uint8Array(analyser.frequencyBinCount);
    if (meterTimer) clearInterval(meterTimer);
    meterTimer = setInterval(() => {
      analyser.getByteTimeDomainData(data);
      let sum = 0;
      for (const v of data) {
        const d = (v - 128) / 128;
        sum += d * d;
      }
      const rms = Math.sqrt(sum / data.length);
      const level = Math.min(100, Math.round(rms * 350));
      el.style.width = level + '%';
      el.classList.toggle('meter-live', level > 2);
    }, 150);
  } catch (_) {
    /* المقياس غير متاح */
  }
}

function renderCall(scenario, status) {
  render(
    topbar(user.name || user.username) +
    `
    <main class="container call-screen">
      <div class="card call-card">
        <div class="call-header">
          <div class="avatar">ع</div>
          <div>
            <h2>عميل سعودي</h2>
            <p class="muted small">${escapeHtml(scenario.customer_role)}</p>
          </div>
          <div class="call-meta">
            <div><span class="dot dot-live"></span> مكالمة مباشرة</div>
            <div>⏱ <span id="call-duration">00:00</span></div>
          </div>
        </div>
        <p id="call-status" class="status">${escapeHtml(status)}</p>
        <div class="meter-wrap" title="صوت العميل">
          <div class="meter" id="audio-meter"></div>
        </div>
        <div class="call-controls">
          <button id="btn-mute" class="btn btn-ghost">🎤 الميكروفون مفعّل</button>
          <button id="btn-end" class="btn btn-danger">📞 إنهاء المكالمة</button>
        </div>
      </div>
    </main>`
  );

  document.getElementById('btn-mute').addEventListener('click', toggleMute);
  document.getElementById('btn-end').addEventListener('click', endCall);
}

function setStatus(text) {
  const el = document.getElementById('call-status');
  if (el) el.textContent = text;
}

function startTimer() {
  const el = document.getElementById('call-duration');
  if (!el) return;
  el.textContent = formatDuration(0);
  current.timer = setInterval(() => {
    el.textContent = formatDuration(Math.floor((Date.now() - current.startedAt) / 1000));
  }, 1000);
}

function stopTimer() {
  if (current && current.timer) clearInterval(current.timer);
}

async function toggleMute() {
  if (!room) return;
  const enabled = room.localParticipant.isMicrophoneEnabled;
  await room.localParticipant.setMicrophoneEnabled(!enabled);
  updateMicUI();
}

function updateMicUI() {
  const btn = document.getElementById('btn-mute');
  if (!btn || !room) return;
  const on = room.localParticipant.isMicrophoneEnabled;
  btn.textContent = on ? '🎤 الميكروفون مفعّل' : '🔇 الميكروفون مكتوم';
  btn.classList.toggle('is-muted', !on);
}

function endCall() {
  renderAnalyzing('عميلك');
  stopTimer();
  const roomName = current ? current.credentials.room : null;
  if (room) {
    try {
      room.disconnect();
    } catch (_) {
      /* تجاهل */
    }
  }
  if (roomName) pollResults(roomName, 0);
}

function onDisconnected() {
  if (meterTimer) {
    clearInterval(meterTimer);
    meterTimer = null;
  }
  if (room) {
    room.removeAllListeners();
    room = null;
  }
}

/* ---------------- التحليل ---------------- */

function renderAnalyzing(customerName) {
  render(
    topbar(user.name || user.username) +
    `
    <main class="container center">
      <div class="card call-card">
        <div class="spinner"></div>
        <h2>جارٍ تحليل مكالمتك…</h2>
        <p class="muted">نراجع محادثتك مع ${escapeHtml(customerName)}. يستغرق هذا بضع ثوانٍ.</p>
      </div>
    </main>`
  );
}

async function pollResults(roomName, attempt) {
  try {
    const data = await api.getResults(roomName);
    if (data.status === 'ready') {
      renderScorecard(data.result);
      return;
    }
  } catch (_) {
    /* نستمر بالتحقق */
  }
  if (attempt < 60) {
    setTimeout(() => pollResults(roomName, attempt + 1), 2000);
  } else {
    renderScorecard(null);
  }
}

/* ---------------- بطاقة التقييم ---------------- */

function renderScorecard(result) {
  if (!result) {
    render(
      topbar(user.name || user.username) +
      `
      <main class="container center">
        <div class="card call-card">
          <h2>بطاقة التقييم غير متوفرة</h2>
          <p class="muted">تعذّر إنشاء بطاقة التقييم لهذه المكالمة. تأكد من أن العميل الآلي يعمل ثم حاول مجددًا.</p>
          <button class="btn btn-primary" id="btn-retry">🔄 إعادة السيناريو</button>
        </div>
      </main>`
    );
    document.getElementById('btn-retry').addEventListener('click', renderLanding);
    return;
  }

  const overall = Math.max(0, Math.min(100, Math.round(Number(result.overall_score) || 0)));
  const scores = result.scores || {};
  const bars = categories
    .map((cat) => {
      const value = Math.max(0, Math.min(10, Math.round(Number(scores[cat.id]) || 0)));
      return `
        <div class="score-row">
          <span class="score-label">${escapeHtml(cat.label)}</span>
          <div class="bar"><div class="bar-fill" style="width:${value * 10}%"></div></div>
          <span class="score-value">${value}/10</span>
        </div>`;
    })
    .join('');

  const list = (items) =>
    Array.isArray(items) && items.length
      ? items.map((i) => `<li>${escapeHtml(i)}</li>`).join('')
      : '<li class="muted">غير متوفر.</li>';

  const transcript = Array.isArray(result.transcript)
    ? result.transcript
        .map(
          (m) => `
          <div class="transcript-line">
            <span class="who ${m.role === 'user' ? 'who-sdr' : 'who-customer'}">
              ${m.role === 'user' ? 'أنت' : escapeHtml(result.customer_name || 'العميل')}
            </span>
            <span>${escapeHtml(m.text)}</span>
          </div>`
        )
        .join('')
    : '<p class="muted">لم يُلتقط نص للمكالمة.</p>';

  const leadMap = {
    mql: { label: 'MQL – عميل مؤهل (ينتقل إلى مندوب المبيعات)', cls: 'lead-mql' },
    follow_up: { label: 'متابعة لاحقة (Follow-up)', cls: 'lead-followup' },
    disqualified: { label: 'غير مؤهل (Disqualified)', cls: 'lead-disq' },
  };
  const lead = leadMap[result.lead_status] || null;
  const leadBlock = lead
    ? `
      <div class="lead-banner ${lead.cls}">
        <strong>${lead.label}</strong>
        ${result.lead_status_reason ? `<span>${escapeHtml(result.lead_status_reason)}</span>` : ''}
      </div>`
    : '';

  render(
    topbar(user.name || user.username) +
    `
    <main class="container">
      <div class="card score-header">
        <div class="overall">
          <div class="overall-ring">
            <span class="overall-num">${overall}</span>
            <span class="overall-max">/100</span>
          </div>
          <div>
            <h2>${escapeHtml(result.scenario_name || 'اكتملت المكالمة')}</h2>
            <p class="muted small">مع ${escapeHtml(result.customer_name || 'العميل')} · ${formatDuration(Number(result.duration_seconds) || 0)}</p>
          </div>
        </div>
        <button class="btn btn-primary" id="btn-retry">🔄 إعادة السيناريو</button>
      </div>

      <div class="grid">
        <div class="card">
          <h3>درجات الفئات</h3>
          ${bars}
          ${leadBlock}
        </div>
        <div>
          <div class="card">
            <h3>⭐ نقاط القوة</h3>
            <ul class="checks">${list(result.strengths)}</ul>
          </div>
          <div class="card">
            <h3>🎯 مجالات التحسين</h3>
            <ul class="warnings">${list(result.areas_for_improvement)}</ul>
          </div>
        </div>
      </div>

      <div class="card">
        <h3>🧠 نصائح المدرب</h3>
        <p>${escapeHtml(result.coaching || 'لا توجد نصائح حالياً.')}</p>
        <h3 class="mt">➡️ الإجراء الموصى به للمحاولة التالية</h3>
        <p class="action">${escapeHtml(result.next_step_action || 'غير متوفر.')}</p>
      </div>

      <div class="card">
        <h3>📝 نص المكالمة</h3>
        <details>
          <summary>عرض النص الكامل</summary>
          <div class="transcript">${transcript}</div>
        </details>
      </div>
    </main>`
  );

  document.getElementById('btn-retry').addEventListener('click', () => {
    stopTimer();
    renderLanding();
  });
}

/* ---------------- Candidate Functions ---------------- */

let candidate = null;
let candidateStream = null;

function getCandidateToken() {
  return localStorage.getItem(CANDIDATE_TOKEN_KEY) || '';
}

function setCandidateToken(t) {
  if (t) localStorage.setItem(CANDIDATE_TOKEN_KEY, t);
  else localStorage.removeItem(CANDIDATE_TOKEN_KEY);
}

function renderCandidateLogin(error = '') {
  render(`
    <header class="topbar">
      <div class="topbar-inner">
        <span class="logo">🎙️</span>
        <h1>AI Simulator - Test Call</h1>
      </div>
    </header>
    <main class="container">
      <div class="card login-card">
        <h2>Candidate Login</h2>
        <p class="muted">Enter your email and candidate ID to start your test call.</p>
        ${error ? `<div class="alert">${escapeHtml(error)}</div>` : ''}
        <form id="candidate-login-form" class="form-grid">
          <label>Email
            <input name="email" type="email" autocomplete="email" required placeholder="e.g. candidate@email.com" />
          </label>
          <label>Candidate ID
            <input name="candidate_id" autocomplete="off" required placeholder="e.g. CAND-XXXXXXXX" />
          </label>
          <button type="submit" class="btn btn-primary full">Login</button>
        </form>
        <p class="muted small" style="margin-top: 1rem;">
          <a href="#" id="switch-to-internal">Login as internal user</a>
        </p>
      </div>
    </main>`);

  document.getElementById('candidate-login-form').addEventListener('submit', (e) => {
    e.preventDefault();
    doCandidateLogin();
  });

  document.getElementById('switch-to-internal').addEventListener('click', (e) => {
    e.preventDefault();
    renderLogin();
  });
}

async function doCandidateLogin() {
  const form = document.getElementById('candidate-login-form');
  const email = form.elements.email.value.trim();
  const candidateId = form.elements.candidate_id.value.trim();

  try {
    const res = await api.post('/api/candidate/login', { email, candidate_id: candidateId });

    if (res.status === 'ended') {
      renderCandidateLogin(res.message || 'You already completed your test call.');
      return;
    }

    if (res.status === 'started') {
      renderCandidateLogin(res.message || 'Test call in progress.');
      return;
    }

    setCandidateToken(res.token);
    candidate = res.candidate;
    renderCandidateReady();
  } catch (err) {
    let msg = err.message || 'Unknown error';
    if (msg.includes('401')) msg = 'Invalid candidate credentials';
    renderCandidateLogin(`Login failed: ${msg}`);
  }
}

function renderCandidateReady(error = '') {
  if (!candidate) {
    renderCandidateLogin();
    return;
  }

  render(`
    <header class="topbar">
      <div class="topbar-inner">
        <span class="logo">🎙️</span>
        <h1>AI Simulator - Test Call</h1>
      </div>
    </header>
    <main class="container">
      <div class="card login-card">
        <h2>Welcome, ${escapeHtml(candidate.candidate_name)}</h2>
        <p class="muted">Ready to start your test call?</p>
        ${error ? `<div class="alert">${escapeHtml(error)}</div>` : ''}
        <div id="camera-preview" style="margin: 1rem 0;">
          <video id="preview-video" autoplay muted playsinline style="width: 100%; max-width: 400px; border-radius: 8px; background: #000;"></video>
        </div>
        <div id="device-status"></div>
        <button id="start-call-btn" class="btn btn-primary full" disabled>Start Test Call</button>
        <button id="logout-btn" class="btn btn-ghost full" style="margin-top: 0.5rem;">Logout</button>
      </div>
    </main>`);

  document.getElementById('logout-btn').addEventListener('click', candidateLogout);
  document.getElementById('start-call-btn').addEventListener('click', startCandidateCall);

  checkCameraAndMic();
}

async function checkCameraAndMic() {
  const video = document.getElementById('preview-video');
  const status = document.getElementById('device-status');
  const startBtn = document.getElementById('start-call-btn');

  try {
    candidateStream = await navigator.mediaDevices.getUserMedia({
      video: true,
      audio: true
    });

    video.srcObject = candidateStream;
    status.innerHTML = '<p style="color: green;">✓ Camera and microphone ready</p>';
    startBtn.disabled = false;

  } catch (err) {
    let message = '';
    if (err.name === 'NotAllowedError') {
      message = 'Camera and microphone access are required to start your Test Call.';
    } else if (err.name === 'NotFoundError') {
      message = 'No camera or microphone detected. Please connect a camera and microphone.';
    } else {
      message = 'Camera and microphone access are required to start your Test Call.';
    }

    status.innerHTML = `<p style="color: red;">${message}</p>`;
    startBtn.disabled = true;
  }
}

async function startCandidateCall() {
  if (!candidate) return;

  const startBtn = document.getElementById('start-call-btn');
  startBtn.disabled = true;
  startBtn.textContent = 'Starting...';

  try {
    const res = await api.candidateStartCall();
    renderCandidateCall(res);
  } catch (err) {
    startBtn.disabled = false;
    startBtn.textContent = 'Start Test Call';
    renderCandidateReady(`Failed to start call: ${err.message}`);
  }
}

function renderCandidateCall(credentials) {
  render(`
    <header class="topbar">
      <div class="topbar-inner">
        <span class="logo">🎙️</span>
        <h1>AI Simulator - Test Call</h1>
      </div>
    </header>
    <main class="container call-screen">
      <div class="card call-card">
        <div class="call-header">
          <div class="avatar">ع</div>
          <div>
            <h2>Test Call</h2>
            <p class="muted small">${escapeHtml(credentials.scenario || '')}</p>
          </div>
          <div class="call-meta">
            <div><span class="dot dot-live"></span> Live</div>
            <div>⏱ <span id="call-duration">00:00</span></div>
          </div>
        </div>
        <p id="call-status" class="status">Connecting...</p>
        <div class="meter-wrap" title="Customer audio">
          <div class="meter" id="audio-meter"></div>
        </div>
        <div class="call-controls">
          <button id="btn-mute" class="btn btn-ghost">🎤 Microphone On</button>
          <button id="btn-end" class="btn btn-danger">📞 End Call</button>
        </div>
      </div>
    </main>`);

  // Connect to LiveKit
  connectCandidateCall(credentials);
}

async function connectCandidateCall(credentials) {
  agentJoined = false;
  audioSubscribed = false;
  room = new Room();

  room.on(RoomEvent.ParticipantConnected, () => {
    agentJoined = true;
    updateCallStatus();
  });

  room.on(RoomEvent.TrackSubscribed, (track) => {
    if (track.kind === 'audio') {
      audioSubscribed = true;
      forcePlayTrack(track);
      attachMeter(track);
      room.startAudio().catch(() => {});
    }
    updateCallStatus();
  });

  room.on(RoomEvent.TrackMuted, updateMicUI);
  room.on(RoomEvent.TrackUnmuted, updateMicUI);
  room.on(RoomEvent.Disconnected, onCandidateDisconnected);

  // Ring tone
  room.on(RoomEvent.DataReceived, (payload, participant, kind, topic) => {
    try {
      const text = typeof payload === 'string' ? payload : new TextDecoder().decode(payload);
      if (text && text.includes('"ring"')) {
        const ringAudio = new Audio('/static/ring.wav');
        ringAudio.volume = 0.8;
        ringAudio.play().catch(() => {});
      }
    } catch (_) {}
  });

  try {
    await room.connect(credentials.url, credentials.token);
    await room.localParticipant.setMicrophoneEnabled(true);

    // Enable camera if available
    if (candidateStream) {
      const videoTrack = candidateStream.getVideoTracks()[0];
      if (videoTrack) {
        await room.localParticipant.publishTrack(videoTrack);
      }
    }

    if (room.remoteParticipants.size > 0) agentJoined = true;
    try {
      await room.startAudio();
    } catch (_) {}

    current = { scenario: { id: credentials.scenario }, credentials, startedAt: Date.now(), timer: null };
    startTimer();
    updateCallStatus();
    updateMicUI();

    document.getElementById('btn-mute').addEventListener('click', toggleMute);
    document.getElementById('btn-end').addEventListener('click', endCandidateCall);

    setTimeout(() => {
      if (!agentJoined && room) {
        setStatus('Waiting for agent...');
      }
    }, 20000);
  } catch (err) {
    try { room.disconnect(); } catch (_) {}
    renderCandidateReady(`Connection failed: ${err.message}`);
  }
}

function onCandidateDisconnected() {
  if (meterTimer) {
    clearInterval(meterTimer);
    meterTimer = null;
  }
  if (room) {
    room.removeAllListeners();
    room = null;
  }
}

function endCandidateCall() {
  stopTimer();
  if (room) {
    try { room.disconnect(); } catch (_) {}
  }
  renderCandidateCompletion();
}

function renderCandidateCompletion() {
  // Stop camera stream
  if (candidateStream) {
    candidateStream.getTracks().forEach(track => track.stop());
    candidateStream = null;
  }

  // Clear candidate session
  setCandidateToken('');
  candidate = null;

  render(`
    <header class="topbar">
      <div class="topbar-inner">
        <span class="logo">🎙️</span>
        <h1>AI Simulator - Test Call</h1>
      </div>
    </header>
    <main class="container center">
      <div class="card call-card">
        <h2>✅ You finished your test call successfully.</h2>
        <p class="muted">You may close this window.</p>
      </div>
    </main>`);
}

function candidateLogout() {
  // Stop camera stream
  if (candidateStream) {
    candidateStream.getTracks().forEach(track => track.stop());
    candidateStream = null;
  }

  setCandidateToken('');
  candidate = null;
  renderCandidateLogin();
}

/* ---------------- Boot ---------------- */

async function boot() {
  // Check for candidate token first
  const candidateToken = getCandidateToken();
  if (candidateToken) {
    try {
      const res = await api.get('/api/candidate/status');
      candidate = {
        candidate_id: res.candidate_id,
        candidate_name: res.candidate_name,
        scenario: res.scenario,
      };
      if (res.status === 'pending') {
        renderCandidateReady();
      } else {
        renderCandidateCompletion();
      }
      return;
    } catch (_) {
      setCandidateToken('');
    }
  }

  // Check for internal user token
  const internalToken = getToken();
  if (internalToken) {
    try {
      const meRes = await api.get('/api/me');
      user = meRes.user;
      await loadRoleData();
      renderLanding();
      return;
    } catch (_) {
      setToken('');
    }
  }

  // Default: show internal login (with link to candidate login)
  renderLogin();
}

boot();