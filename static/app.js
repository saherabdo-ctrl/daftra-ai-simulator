import { Room, RoomEvent } from 'livekit-client';

// Loading overlay functions
function showLoading(message = 'جاري التحميل...') {
  const existing = document.getElementById('loading-overlay');
  if (existing) existing.remove();
  
  const overlay = document.createElement('div');
  overlay.id = 'loading-overlay';
  overlay.className = 'loading-overlay';
  overlay.innerHTML = `
    <div class="loading-spinner"></div>
    <div class="loading-text">${message}</div>
  `;
  document.body.appendChild(overlay);
}

function hideLoading() {
  const overlay = document.getElementById('loading-overlay');
  if (overlay) overlay.remove();
}

// Set button loading state
function setBtnLoading(btn, loading, text) {
  if (!btn) return;
  if (loading) {
    btn.classList.add('loading');
    btn.dataset.originalText = btn.textContent;
    btn.textContent = text || btn.textContent;
  } else {
    btn.classList.remove('loading');
    if (btn.dataset.originalText) {
      btn.textContent = btn.dataset.originalText;
    }
  }
}

const TOKEN_KEY = 'sdr_token';
const CANDIDATE_TOKEN_KEY = 'candidate_token';
function roleLabel(role) {
  return t('role_' + role) !== ('role_' + role) ? t('role_' + role) : role;
}

// الصلاحيات بقت مخزّنة لكل مستخدم على حدة (checkboxes)، مفيش "role = admin" واحد
// بيفتح كل حاجة. "أدمن-مثل" هنا معناها: عنده أي صلاحية إدارية غير إجراء مكالمة بس،
// فيستاهل يشوف لوحة التحكم بدل شاشة المكالمة العادية.
function isAdminLike(u) {
  const perms = u?.permissions || {};
  return Object.keys(perms).some((key) => key !== 'make_calls' && perms[key]);
}

function getToken() {
  // Return the token for the currently active flow
  // If candidate is active, use candidate token; otherwise use internal token
  if (candidate) return localStorage.getItem(CANDIDATE_TOKEN_KEY) || '';
  return localStorage.getItem(TOKEN_KEY) || '';
}
function setToken(t) {
  if (t) localStorage.setItem(TOKEN_KEY, t);
  else localStorage.removeItem(TOKEN_KEY);
}
function setCandidateToken(t) {
  if (t) localStorage.setItem(CANDIDATE_TOKEN_KEY, t);
  else localStorage.removeItem(CANDIDATE_TOKEN_KEY);
}

const api = {
  async request(path, options = {}) {
    const headers = options.headers || {};
    const token = getToken();
    if (token) headers['Authorization'] = `Bearer ${token}`;
    const res = await fetch(path, { ...options, headers });
    // A 401 from the login endpoints themselves just means wrong credentials —
    // not an expired session — so let the caller's own error handling show it.
    const isLoginAttempt = path === '/api/login' || path === '/api/candidate/login';
    if (res.status === 401 && !isLoginAttempt) {
      const candidateToken = localStorage.getItem(CANDIDATE_TOKEN_KEY);
      setToken('');
      setCandidateToken('');
      renderLogin(candidateToken ? 'Session expired — please login again' : 'انتهت الجلسة — سجّل الدخول مجددًا');
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
  put: (path, body) =>
    api.request(path, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  upload: (path, file) => {
    const fd = new FormData();
    fd.append('file', file);
    return api.request(path, { method: 'POST', body: fd });
  },
  login: (username, password) => api.post('/api/login', { username, password }),
  logout: () => api.post('/api/logout', {}),
  me: () => api.get('/api/me'),
  getScenarios: () => api.get('/api/scenarios'),
  getToken: (scenario) => api.post('/api/token', { scenario }),
  getResults: (room) => api.get(`/api/results/${encodeURIComponent(room)}`),
  createCustom: (brief) => api.post('/api/scenarios/custom', brief),
  uploadCall: (file) => api.upload('/api/upload-call', file),
  randomCall: () => api.post('/api/random-call', {}),
  // Candidate endpoints
  candidateLogin: (email, candidate_id) =>
    api.post('/api/candidate/login', { email, candidate_id }),
  candidateStartCall: (attemptId) => api.post('/api/candidate/start-call', { attempt_id: attemptId || '' }),
  candidateEndCall: () => api.post('/api/candidate/end-call', {}),
  candidateStatus: () => api.get('/api/candidate/status'),
  // Classification endpoints
  getClassifications: () => api.get('/api/classifications'),
  createClassification: (data) => api.post('/api/classifications', data),
  updateClassification: (id, data) => api.request(`/api/classifications/${encodeURIComponent(id)}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data),
  }),
  deleteClassification: (id) => api.del(`/api/classifications/${encodeURIComponent(id)}`),
  resetClassificationAIClients: (id) => api.post(`/api/classifications/${encodeURIComponent(id)}/reset-ai-clients`, {}),
  // AI Client endpoints
  getAIClients: (classificationId) => api.get(`/api/ai-clients${classificationId ? `?classification_id=${encodeURIComponent(classificationId)}` : ''}`),
  createAIClient: (data) => api.post('/api/ai-clients', data),
  updateAIClient: (id, data) => api.request(`/api/ai-clients/${encodeURIComponent(id)}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data),
  }),
  deleteAIClient: (id) => api.del(`/api/ai-clients/${encodeURIComponent(id)}`),
  generateAIClients: (count, classification_id, country) => api.post('/api/ai-clients/generate', { count, classification_id, country }),
  // Attempt endpoints
  createAttempt: (data) => api.post('/api/attempts', data),
  getAttempt: (id) => api.get(`/api/attempts/${encodeURIComponent(id)}`),
  consumeAttempt: (id) => api.post(`/api/attempts/${encodeURIComponent(id)}/consume`, {}),
  // User management endpoints
  getUsers: () => api.get('/api/users'),
  getUserMeta: () => api.get('/api/user-meta'),
  createUser: (data) => api.post('/api/users', data),
  updateUser: (username, data) => api.put(`/api/users/${encodeURIComponent(username)}`, data),
  deleteUser: (username) => api.del(`/api/users/${encodeURIComponent(username)}`),
  bulkUsers: (names, base, role) => api.post('/api/users/bulk', { names, base_username: base, role }),
  // Test session endpoints
  getTestSessions: (classificationId) => api.get(`/api/test-sessions${classificationId ? `?classification_id=${encodeURIComponent(classificationId)}` : ''}`),
  getTestSession: (id) => api.get(`/api/test-sessions/${encodeURIComponent(id)}`),
  revokeTestSession: (id) => api.post(`/api/test-sessions/${encodeURIComponent(id)}/revoke`, {}),
  getLinkSettings: () => api.get('/api/link-settings'),
  updateLinkSettings: (maxRescheduleCount) => api.post('/api/link-settings', { max_reschedule_count: maxRescheduleCount }),
  // Results endpoints
  getResultsList: () => api.get('/api/results'),
  deleteResult: (room) => api.del(`/api/results/${encodeURIComponent(room)}`),
  bulkDeleteResults: (rooms) => api.post('/api/results/bulk-delete', { rooms }),
  getKnowledgeBase: () => api.get('/api/knowledge-base'),
  saveKnowledgeBase: (content) => api.post('/api/knowledge-base', { content }),
  // Admin start call
  adminStartCall: (ai_client_id) => api.post('/api/admin/start-call', { ai_client_id }),
  getCallsList: (classification_id) => api.get(classification_id ? `/api/calls?classification_id=${classification_id}` : '/api/calls'),
  // Diagnostic
  getDiagnostic: () => api.get('/api/diagnostic'),
};

const app = document.getElementById('app');

let user = null;
let room = null;
let scenarios = [];
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

/* ---------------- i18n (system/UI language — NEVER translates call content:
   scenario/persona text, transcripts, evaluation feedback stay Arabic as-is) ---------------- */

function getLanguage() {
  try { return localStorage.getItem('daftraai_language') || 'ar'; } catch (_) { return 'ar'; }
}

// Every UI-chrome string added from now on MUST get an entry here (both ar and
// en) — new text added without one will just silently stay Arabic in English
// mode instead of crashing, so it's easy to miss if you forget.
const TRANSLATIONS = {
  // Shared brand/topbar
  brand_name: { ar: 'Sales Heroes Arena', en: 'Sales Heroes Arena' },

  // Login (internal) + Trial link + Scheduled test-call link
  login_title: { ar: 'تسجيل الدخول', en: 'Login' },
  login_subtitle: { ar: 'ادخل بريدك الإلكتروني وكود الدخول.', en: 'Enter your email and access code.' },
  login_email_label: { ar: 'البريد الإلكتروني', en: 'Email' },
  login_code_label: { ar: 'كود الدخول', en: 'Access Code' },
  login_button: { ar: 'دخول', en: 'Login' },
  login_email_placeholder: { ar: 'مثال: user@company.com', en: 'e.g. user@company.com' },
  login_code_placeholder: { ar: 'مثال: HF-XXXXXXXX', en: 'e.g. HF-XXXXXXXX' },

  // Admin nav
  nav_dashboard: { ar: '🏠 لوحة التحكم', en: '🏠 Dashboard' },
  nav_classifications: { ar: '📂 التصنيفات', en: '📂 Classifications' },
  nav_ai_clients: { ar: '🤖 العملاء الذكيين', en: '🤖 AI Clients' },
  nav_calls: { ar: '📞 المكالمات', en: '📞 Calls' },
  nav_test_sessions: { ar: '📋 جلسات الاختبار', en: '📋 Test Sessions' },
  nav_results: { ar: '📊 النتائج', en: '📊 Results' },
  nav_settings: { ar: '⚙️ الإعدادات', en: '⚙️ Settings' },
  nav_logout: { ar: 'خروج', en: 'Logout' },

  // Dashboard home
  dashboard_title: { ar: 'لوحة التحكم', en: 'Dashboard' },
  dashboard_welcome: { ar: 'مرحبًا {name} 👋', en: 'Welcome {name} 👋' },
  stat_classifications: { ar: 'التصنيفات', en: 'Classifications' },
  stat_ai_clients: { ar: 'العملاء الذكيين', en: 'AI Clients' },
  stat_test_sessions: { ar: 'جلسات الاختبار', en: 'Test Sessions' },
  stat_users: { ar: 'المستخدمين', en: 'Users' },
  home_card_classifications_title: { ar: '📂 التصنيفات', en: '📂 Classifications' },
  home_card_classifications_desc: { ar: 'إدارة التصنيفات والعملاء الذكيين', en: 'Manage classifications and AI clients' },
  home_card_classifications_btn: { ar: 'عرض التصنيفات', en: 'View Classifications' },
  home_card_test_sessions_title: { ar: '📞 جلسات الاختبار', en: '📞 Test Sessions' },
  home_card_test_sessions_desc: { ar: 'مراقبة وإدارة جلسات الاختبار', en: 'Monitor and manage test sessions' },
  home_card_test_sessions_btn: { ar: 'عرض الجلسات', en: 'View Sessions' },
  home_card_results_title: { ar: '📊 النتائج', en: '📊 Results' },
  home_card_results_desc: { ar: 'عرض نتائج التقييمات', en: 'View evaluation results' },
  home_card_results_btn: { ar: 'عرض النتائج', en: 'View Results' },

  // Settings — language section
  settings_title: { ar: '⚙️ الإعدادات', en: '⚙️ Settings' },
  settings_language_title: { ar: '🌐 اللغة', en: '🌐 Language' },
  settings_language_desc: { ar: 'تغيير لغة واجهة النظام (محتوى المكالمات نفسه يفضل عربي دايمًا)', en: 'Change the system interface language (call content itself always stays Arabic)' },
  settings_save: { ar: '💾 حفظ الإعدادات', en: '💾 Save Settings' },
  settings_saved: { ar: 'تم حفظ الإعدادات ✅', en: 'Settings saved ✅' },
  settings_dirty: { ar: 'فيه تعديل لسه ما اتحفظش', en: 'You have unsaved changes' },

  // Shared option labels (difficulty / personality / knowledgeable — reused across
  // the Classification and AI Client forms)
  opt_random: { ar: '🎲 عشوائي', en: '🎲 Random' },
  opt_easy: { ar: 'سهل', en: 'Easy' },
  opt_medium: { ar: 'متوسط', en: 'Medium' },
  opt_hard: { ar: 'صعب', en: 'Hard' },
  opt_by_difficulty: { ar: '— حسب الصعوبة —', en: '— By difficulty —' },
  opt_friendly: { ar: 'ودود', en: 'Friendly' },
  opt_busy: { ar: 'مشغول', en: 'Busy' },
  opt_hesitant: { ar: 'متردد', en: 'Hesitant' },
  opt_price_sensitive: { ar: 'حساس للسعر', en: 'Price-sensitive' },
  opt_skeptical: { ar: 'متشكك', en: 'Skeptical' },
  opt_angry: { ar: 'مزعوج', en: 'Angry' },
  opt_confused: { ar: 'محتار', en: 'Confused' },
  opt_interested: { ar: 'متحمس', en: 'Interested' },
  opt_low_intent: { ar: 'غير مهتم', en: 'Low intent' },
  opt_competitor: { ar: 'مستخدم منافس', en: 'Uses a competitor' },
  opt_difficult: { ar: 'صعب', en: 'Difficult' },
  opt_indecisive: { ar: 'ما يعرف يقرر', en: 'Indecisive' },
  opt_knowledgeable_true: { ar: 'محاسب/فاهم في المحاسبة', en: 'Accountant / knows accounting' },
  opt_knowledgeable_false: { ar: 'عميل عادي', en: 'Regular customer' },
  save: { ar: '💾 حفظ', en: '💾 Save' },
  cancel: { ar: 'إلغاء', en: 'Cancel' },

  // Classifications
  classifications_title: { ar: '📂 التصنيفات', en: '📂 Classifications' },
  classifications_add_btn: { ar: '+ إضافة تصنيف', en: '+ Add Classification' },
  classifications_empty: { ar: 'لا يوجد تصنيفات بعد.', en: 'No classifications yet.' },
  classification_form_title_add: { ar: 'إضافة تصنيف', en: 'Add Classification' },
  classification_form_title_edit: { ar: 'تعديل التصنيف', en: 'Edit Classification' },
  classification_name_label: { ar: 'اسم التصنيف *', en: 'Classification Name *' },
  classification_name_placeholder: { ar: 'مثال: EG Sales', en: 'e.g. EG Sales' },
  classification_description_label: { ar: 'الوصف', en: 'Description' },
  classification_description_placeholder: { ar: 'وصف مختصر للتصنيف', en: 'A short description of this classification' },
  classification_min_duration_label: { ar: 'مدة المكالمة من (دقيقة)', en: 'Call duration from (minutes)' },
  classification_max_duration_label: { ar: 'مدة المكالمة إلى (دقيقة)', en: 'Call duration to (minutes)' },
  classification_optional_placeholder: { ar: 'اختياري', en: 'Optional' },
  classification_default_difficulty_label: { ar: 'صعوبة العملاء (افتراضي لكل عملاء التصنيف)', en: "Clients' difficulty (default for all clients in this classification)" },
  classification_default_personality_label: { ar: 'شخصية العملاء (افتراضي لكل عملاء التصنيف)', en: "Clients' personality (default for all clients in this classification)" },
  classification_default_knowledgeable_label: { ar: 'مدى المعلومات (افتراضي لكل عملاء التصنيف)', en: 'Knowledge level (default for all clients in this classification)' },
  classification_no_description: { ar: 'بدون وصف', en: 'No description' },
  classification_clients_count: { ar: 'عملاء', en: 'clients' },
  classification_view_clients_btn: { ar: 'عرض العملاء', en: 'View Clients' },
  classification_edit_btn: { ar: 'تعديل', en: 'Edit' },
  classification_reset_btn: { ar: '↺ Reset العملاء', en: '↺ Reset Clients' },
  classification_reset_title: { ar: 'يرجع كل عملاء التصنيف ده لإعدادات الصعوبة/الشخصية/المعرفة الافتراضية بتاعة التصنيف', en: "Resets every client in this classification back to its default difficulty/personality/knowledge settings" },
  classification_delete_btn: { ar: 'حذف', en: 'Delete' },
  classification_reset_confirm: { ar: 'هيتم رجوع كل عملاء التصنيف ده (الصعوبة/الشخصية/مدى المعلومات) لإعدادات التصنيف الافتراضية الحالية. متأكد؟', en: "Every client in this classification will be reset (difficulty/personality/knowledge) to the classification's current defaults. Are you sure?" },
  classification_reset_success: { ar: 'تم تحديث {count} عميل ✅', en: '{count} client(s) updated ✅' },
  classification_reset_failed: { ar: 'فشل إعادة الضبط: {error}', en: 'Failed to reset: {error}' },
  classification_delete_confirm: { ar: 'هل أنت متأكد من حذف هذا التصنيف؟', en: 'Are you sure you want to delete this classification?' },
  classification_delete_failed: { ar: 'فشل حذف التصنيف: {error}', en: 'Failed to delete classification: {error}' },
  classification_save_failed: { ar: 'فشل حفظ التصنيف: {error}', en: 'Failed to save classification: {error}' },

  // Generic action-status strings reused across many screens
  status_resetting: { ar: 'جاري إعادة الضبط...', en: 'Resetting...' },
  status_deleting: { ar: 'جاري الحذف...', en: 'Deleting...' },
  status_saving: { ar: 'جاري الحفظ...', en: 'Saving...' },
  status_creating: { ar: 'جاري الإنشاء...', en: 'Creating...' },
  status_updating: { ar: 'جاري التحديث...', en: 'Updating...' },
  status_loading: { ar: 'جاري التحميل...', en: 'Loading...' },

  // AI Clients
  ai_clients_title: { ar: '🤖 العملاء الذكيين', en: '🤖 AI Clients' },
  ai_clients_generate_btn: { ar: '🪄 توليد عملاء بالذكاء الاصطناعي', en: '🪄 Generate Clients with AI' },
  ai_clients_add_btn: { ar: '+ إضافة عميل', en: '+ Add Client' },
  ai_clients_filter_label: { ar: 'التصنيف:', en: 'Classification:' },
  ai_clients_filter_all: { ar: '— جميع التصنيفات —', en: '— All Classifications —' },
  ai_clients_empty: { ar: 'لا يوجد عملاء ذكيين بعد.', en: 'No AI clients yet.' },
  generate_modal_title: { ar: '🪄 توليد عملاء بالذكاء الاصطناعي', en: '🪄 Generate Clients with AI' },
  generate_modal_desc: { ar: 'هيتولد عملاء مختلفين تمامًا عن بعض (النشاط، الحجم، المشكلة...) بناءً على قاعدة معرفة دفترة. الصعوبة والشخصية بتفضل عشوائية دايمًا لكل عميل.', en: 'Generates completely different clients from each other (activity, size, problem...) based on the Daftra knowledge base. Difficulty and personality always stay random per client.' },
  generate_count_label: { ar: 'عدد العملاء (1-50) *', en: 'Number of clients (1-50) *' },
  generate_country_label: { ar: 'الدولة', en: 'Country' },
  generate_country_both: { ar: '🇸🇦🇪🇬 الاتنين', en: '🇸🇦🇪🇬 Both' },
  generate_country_eg: { ar: '🇪🇬 مصر فقط', en: '🇪🇬 Egypt only' },
  generate_country_sa: { ar: '🇸🇦 السعودية فقط', en: '🇸🇦 Saudi Arabia only' },
  generate_classification_label: { ar: 'التصنيف', en: 'Classification' },
  generate_no_classification: { ar: '— بدون تصنيف —', en: '— No classification —' },
  generate_submit_btn: { ar: '🪄 توليد', en: '🪄 Generate' },
  ai_client_form_title_add: { ar: 'إضافة عميل ذكي', en: 'Add AI Client' },
  ai_client_form_title_edit: { ar: 'تعديل العميل الذكي', en: 'Edit AI Client' },
  ai_client_name_label: { ar: 'اسم البروفايل *', en: 'Profile Name *' },
  ai_client_name_placeholder: { ar: 'مثال: فهد - مؤسسة أدوية', en: 'e.g. Fahad - Pharmacy Est.' },
  ai_client_status_label: { ar: 'الحالة', en: 'Status' },
  ai_client_status_active: { ar: '✅ نشط', en: '✅ Active' },
  ai_client_status_inactive: { ar: '⛔ غير نشط', en: '⛔ Inactive' },
  ai_client_language_label: { ar: 'لغة العميل', en: 'Customer Language' },
  ai_client_classification_label: { ar: 'التصنيف', en: 'Classification' },
  ai_client_scenario_label: { ar: 'السيناريو', en: 'Scenario' },
  scenario_discovery: { ar: 'عميل جديد — اكتشاف', en: 'New customer — Discovery' },
  scenario_followup: { ar: 'عميل سابق — متابعة', en: 'Past customer — Follow-up' },
  scenario_demo: { ar: 'عميل مهتم — عرض تجريبي', en: 'Interested customer — Demo' },
  ai_client_country_label: { ar: 'الدولة (تحدد اللهجة والصوت والتسعير)', en: 'Country (determines dialect, voice, and pricing)' },
  ai_client_queue_label: { ar: 'Queue', en: 'Queue' },
  ai_client_queue_none: { ar: '— بدون —', en: '— None —' },
  ai_client_voice_label: { ar: 'الصوت', en: 'Voice' },
  ai_client_voice_auto: { ar: '— تلقائي حسب اللهجة —', en: '— Automatic by dialect —' },
  ai_client_difficulty_label: { ar: 'الصعوبة', en: 'Difficulty' },
  ai_client_customer_name_label: { ar: 'اسم العميل في المكالمة', en: "Customer's name in the call" },
  ai_client_customer_name_placeholder: { ar: 'مثال: فهد العتيبي', en: 'e.g. Fahad Al-Otaibi' },
  ai_client_customer_role_label: { ar: 'منصب العميل', en: "Customer's role" },
  ai_client_customer_role_placeholder: { ar: 'مثال: مدير المشتريات', en: 'e.g. Purchasing Manager' },
  ai_client_company_name_label: { ar: 'اسم الشركة', en: 'Company Name' },
  ai_client_company_name_placeholder: { ar: 'مثال: شركة الشفاء للأدوية', en: 'e.g. Al-Shifa Pharma Co.' },
  ai_client_business_field_label: { ar: 'مجال عمل العميل', en: "Customer's business field" },
  ai_client_business_field_placeholder: { ar: 'مثال: توزيع أدوية بالجملة للصيدليات', en: 'e.g. Wholesale medicine distribution to pharmacies' },
  ai_client_company_size_label: { ar: 'حجم الشركة', en: 'Company Size' },
  ai_client_company_size_placeholder: { ar: 'مثال: 50 موظفًا، 3 فروع', en: 'e.g. 50 employees, 3 branches' },
  ai_client_pain_point_label: { ar: 'نقطة الألم / المشكلة الأساسية', en: 'Pain point / core problem' },
  ai_client_pain_point_placeholder: { ar: 'مثال: إدارة المخزون بجداول إكسل يدويًا، خسائر بسبب عدم مطابقة المخزون', en: 'e.g. Manual inventory management in Excel, losses from stock mismatches' },
  ai_client_decision_maker_label: { ar: 'صاحب القرار', en: 'Decision Maker' },
  ai_client_decision_maker_yes: { ar: 'نعم، هو من يقرر', en: 'Yes, they decide' },
  ai_client_decision_maker_no: { ar: 'لا، لست صاحب القرار', en: "No, they aren't the decision maker" },
  ai_client_personality_label: { ar: 'الشخصية', en: 'Personality' },
  ai_client_temperature_label: { ar: 'درجة حرارة العميل', en: "Customer's temperature" },
  ai_client_temperature_warm: { ar: 'يعرفنا (warm)', en: 'Knows us (warm)' },
  ai_client_temperature_cold: { ar: 'لا يعرفنا (cold)', en: "Doesn't know us (cold)" },
  ai_client_budget_sensitivity_label: { ar: 'الحساسية للميزانية', en: 'Budget Sensitivity' },
  ai_client_buying_intent_label: { ar: 'نية الشراء', en: 'Buying Intent' },
  level_low: { ar: 'منخفضة', en: 'Low' },
  level_medium: { ar: 'متوسطة', en: 'Medium' },
  level_high: { ar: 'عالية', en: 'High' },
  ai_client_knowledgeable_label: { ar: 'معرفة العميل بالمحاسبة', en: "Customer's accounting knowledge" },
  ai_client_instructions_label: { ar: 'تعليمات للعميل الذكي', en: 'Instructions for the AI client' },
  ai_client_instructions_placeholder: { ar: 'مثال: لا تذكر الأسعار إلا إذا سأل، تحدث عن الميزات فقط، لا تذكر المنافسين', en: "e.g. Don't mention prices unless asked, only talk about features, don't mention competitors" },
  ai_client_objectives_label: { ar: 'هدف العميل من المكالمة', en: "Customer's goal for the call" },
  ai_client_objectives_placeholder: { ar: 'مثال: يبي يعرف لو النظام يدعم إدارة الصلاحيات لكل فرع، يبي يعرف التسعير', en: 'e.g. Wants to know if the system supports per-branch permissions, wants pricing' },
  ai_client_objections_label: { ar: 'اعتراضات العميل المحتملة', en: "Customer's likely objections" },
  ai_client_objections_placeholder: { ar: 'مثال: غالي، ما عندي وقت، أنا جربت نظام ثاني و ما ناسبني، أذاكرها مع مدير المشتريات', en: "e.g. Too expensive, no time, tried another system and it didn't work, needs to discuss with purchasing manager" },
  ai_client_product_brief_label: { ar: 'منتج/خدمة العميل (اللي بيبيعه)', en: "The customer's own product/service (what they sell)" },
  ai_client_product_brief_placeholder: { ar: 'مثال: شركة شفاء توزع أدوية بالجملة على 200 صيدلية في المنطقة الغربية، عندها مخزون 5000 صنف', en: 'e.g. Al-Shifa distributes medicine wholesale to 200 pharmacies in the western region, with 5000 SKUs in stock' },
  ai_client_save_btn: { ar: '💾 حفظ العميل', en: '💾 Save Client' },
  ai_client_save_failed: { ar: 'فشل حفظ العميل: {error}', en: 'Failed to save client: {error}' },
  ai_client_call_btn: { ar: '📞 مكالمة', en: '📞 Call' },
  ai_client_delete_confirm: { ar: 'هل أنت متأكد من حذف هذا العميل الذكي؟', en: 'Are you sure you want to delete this AI client?' },
  ai_client_delete_failed: { ar: 'فشل حذف العميل الذكي: {error}', en: 'Failed to delete AI client: {error}' },
  ai_client_default_name: { ar: 'العميل', en: 'the client' },
  ai_client_start_call_confirm: { ar: 'ابدأ مكالمة مع "{name}"؟', en: 'Start a call with "{name}"?' },
  status_connecting: { ar: 'جاري الاتصال...', en: 'Connecting...' },
  ai_client_start_call_failed: { ar: 'فشل بدء المكالمة: {error}', en: 'Failed to start call: {error}' },
  generate_in_progress: { ar: 'جاري التوليد... (قد تستغرق دقيقة)', en: 'Generating... (may take a minute)' },
  generate_skipped_duplicates: { ar: '({count} اتجاهلوا لأنهم مكررين)', en: '({count} skipped as duplicates)' },
  generate_success: { ar: 'تم توليد {created} من {requested} عميل بنجاح ✅', en: 'Successfully generated {created} of {requested} clients ✅' },
  generate_failed: { ar: 'فشل توليد العملاء: {error}', en: 'Failed to generate clients: {error}' },
  country_sa: { ar: '🇸🇦 السعودية', en: '🇸🇦 Saudi Arabia' },
  country_eg: { ar: '🇪🇬 مصر', en: '🇪🇬 Egypt' },

  // Test Sessions / link generation
  test_sessions_title: { ar: '📞 جلسات الاختبار', en: '📞 Test Sessions' },
  test_sessions_create_link_btn: { ar: '+ توليد رابط اختبار', en: '+ Generate Test Link' },
  link_settings_title: { ar: '⚙️ إعدادات إعادة توليد اللينك التلقائي', en: '⚙️ Auto Link Regeneration Settings' },
  link_settings_desc: { ar: 'الحد الأقصى لعدد مرات تأجيل الميعاد (RescheduleCount) اللي لسه بيسمح بتوليد لينك جديد للمرشح تلقائيًا بعدها. اكتب -1 لعدد غير محدود.', en: "The max number of times a candidate can reschedule (RescheduleCount) and still get a new link auto-generated. Enter -1 for unlimited." },
  link_settings_max_label: { ar: 'الحد الأقصى', en: 'Maximum' },
  test_sessions_empty: { ar: 'لا توجد جلسات اختبار بعد.', en: 'No test sessions yet.' },
  link_gen_modal_title: { ar: 'توليد رابط اختبار', en: 'Generate Test Link' },
  link_gen_candidate_name_label: { ar: 'اسم المرشح *', en: 'Candidate Name *' },
  link_gen_candidate_name_placeholder: { ar: 'مثال: أحمد محمد', en: 'e.g. Ahmed Mohamed' },
  link_gen_candidate_email_label: { ar: 'بريد المرشح *', en: 'Candidate Email *' },
  link_gen_candidate_email_placeholder: { ar: 'مثال: ahmed@example.com', en: 'e.g. ahmed@example.com' },
  link_gen_candidate_id_label: { ar: 'معرف المرشح (CandidateID)', en: 'Candidate ID' },
  link_gen_candidate_id_placeholder: { ar: 'اتركه فارغًا للإنشاء التلقائي', en: 'Leave blank for auto-generation' },
  link_gen_classification_label: { ar: 'التصنيف', en: 'Classification' },
  link_gen_classification_placeholder: { ar: '— اختر تصنيف —', en: '— Choose a classification —' },
  link_gen_ai_client_label: { ar: 'العميل الذكي', en: 'AI Client' },
  link_gen_ai_client_placeholder: { ar: '— جميع العملاء (اختيار عشوائي) —', en: '— All clients (random) —' },
  link_gen_submit_btn: { ar: '🔗 توليد الرابط', en: '🔗 Generate Link' },
  link_gen_result_label: { ar: 'رابط الاختبار (انسخه وأرسله للمرشح):', en: 'Test link (copy and send it to the candidate):' },
  link_gen_copy_btn: { ar: '📋 نسخ الرابط', en: '📋 Copy Link' },

  unspecified: { ar: 'غير محدد', en: 'Unspecified' },
  random_choice: { ar: 'اختيار عشوائي', en: 'Random' },
  status_pending: { ar: 'معلق', en: 'Pending' },
  status_used: { ar: 'مستخدم', en: 'Used' },
  status_completed: { ar: 'مكتمل', en: 'Completed' },
  test_session_email_label: { ar: 'البريد', en: 'Email' },
  test_session_id_label: { ar: 'المعرف', en: 'ID' },
  test_session_classification_label: { ar: 'التصنيف', en: 'Classification' },
  test_session_ai_client_label: { ar: 'العميل الذكي', en: 'AI Client' },
  revoke_link_confirm: { ar: 'هل أنت متأكد من إلغاء هذا الرابط؟', en: 'Are you sure you want to revoke this link?' },
  revoke_link_failed: { ar: 'فشل إلغاء الرابط: {error}', en: 'Failed to revoke link: {error}' },
  link_settings_invalid_value: { ar: 'قيمة غير صالحة — لازم يكون -1 أو أكبر.', en: 'Invalid value — must be -1 or greater.' },
  link_settings_saved: { ar: 'تم الحفظ ✅', en: 'Saved ✅' },
  link_settings_save_failed: { ar: 'فشل الحفظ: {error}', en: 'Save failed: {error}' },

  // Calls view
  calls_title: { ar: '📞 المكالمات', en: '📞 Calls' },
  calls_all_classifications: { ar: '— جميع التصنيفات —', en: '— All classifications —' },
  calls_loading: { ar: 'جاري تحميل المكالمات...', en: 'Loading calls...' },
  calls_empty: { ar: 'لا توجد مكالمات بعد.', en: 'No calls yet.' },
  call_default_client_name: { ar: 'عميل ذكي', en: 'AI Client' },
  status_started: { ar: 'جارية', en: 'In progress' },
  call_id_label: { ar: 'المعرف', en: 'ID' },
  call_classification_label: { ar: 'التصنيف', en: 'Classification' },
  call_caller_label: { ar: 'المصلح', en: 'Trainee' },
  call_started_label: { ar: 'البداية', en: 'Started' },
  call_ended_label: { ar: 'النهاية', en: 'Ended' },
  call_duration_label: { ar: 'المدة', en: 'Duration' },
  call_duration_seconds: { ar: '{seconds} ثانية', en: '{seconds} sec' },
  call_score_label: { ar: 'الدرجة', en: 'Score' },
  call_download_pdf_btn: { ar: '📄 تحميل PDF', en: '📄 Download PDF' },

  // Results view
  results_title: { ar: '📊 النتائج', en: '📊 Results' },
  results_select_all: { ar: 'تحديد الكل', en: 'Select all' },
  results_delete_selected_btn: { ar: '🗑️ حذف المحدد ({count})', en: '🗑️ Delete selected ({count})' },
  results_empty: { ar: 'لا توجد نتائج بعد.', en: 'No results yet.' },

  // Settings — remaining sections
  settings_users_title: { ar: '👥 إدارة المستخدمين', en: '👥 User Management' },
  settings_users_desc: { ar: 'إضافة وحذف المستخدمين والأدوار', en: 'Add and remove users and roles' },
  settings_users_manage_btn: { ar: 'إدارة المستخدمين', en: 'Manage Users' },
  settings_links_title: { ar: '🔗 روابط الاختبار', en: '🔗 Test Links' },
  settings_links_desc: { ar: 'توليد وإدارة روابط الاختبار', en: 'Generate and manage test links' },
  settings_links_manage_btn: { ar: 'إدارة الروابط', en: 'Manage Links' },
  settings_diagnostic_title: { ar: '📊 التشخيص', en: '📊 Diagnostics' },
  settings_diagnostic_desc: { ar: 'فحص حالة النظام', en: 'Check system status' },
  settings_diagnostic_run_btn: { ar: 'تشغيل التشخيص', en: 'Run Diagnostics' },
  settings_knowledge_title: { ar: '📚 قاعدة معرفة دفترة', en: '📚 Daftra Knowledge Base' },
  settings_knowledge_desc: { ar: 'معلومات عامة (غير الأسعار — دي بتتحدث تلقائيًا من الموقع يوميًا) يستخدمها الـ AI Simulator في شخصية العميل الذكي وفي تقييم المندوب.', en: 'General information (not pricing — that auto-updates from the website daily) used by the AI Simulator in the AI client persona and in trainee evaluation.' },
  settings_knowledge_placeholder: { ar: 'جاري التحميل...', en: 'Loading...' },
  settings_knowledge_save_btn: { ar: '💾 حفظ قاعدة المعرفة', en: '💾 Save Knowledge Base' },
  status_checking: { ar: 'جاري الفحص...', en: 'Checking...' },
  diagnostic_failed: { ar: 'فشل التشخيص: {error}', en: 'Diagnostics failed: {error}' },
  knowledge_load_failed: { ar: 'تعذّر تحميل المحتوى الحالي', en: 'Failed to load current content' },
  knowledge_saved: { ar: 'تم الحفظ ✅', en: 'Saved ✅' },
  knowledge_save_failed: { ar: 'فشل حفظ قاعدة المعرفة: {error}', en: 'Failed to save knowledge base: {error}' },

  // Roles
  role_sdr: { ar: 'مندوب مبيعات', en: 'Sales Rep' },
  role_quality: { ar: 'فريق الجودة', en: 'Quality Team' },
  role_quality_manager: { ar: 'مدير الجودة', en: 'Quality Manager' },
  role_quality_specialist: { ar: 'أخصائي الجودة', en: 'Quality Specialist' },
  role_admin: { ar: 'مدير', en: 'Admin' },
  role_ta_manager: { ar: 'مدير التوظيف', en: 'TA Manager' },
  role_ta_team_leader: { ar: 'قائد فريق التوظيف', en: 'TA Team Leader' },

  // Permissions
  perm_manage_users: { ar: '👥 إدارة المستخدمين', en: '👥 Manage Users' },
  perm_manage_classifications: { ar: '📂 إدارة التصنيفات', en: '📂 Manage Classifications' },
  perm_manage_ai_clients: { ar: '🤖 إدارة العملاء الذكيين', en: '🤖 Manage AI Clients' },
  perm_delete_calls_results: { ar: '🗑️ حذف المكالمات/النتائج', en: '🗑️ Delete Calls/Results' },
  perm_view_results: { ar: '📊 عرض النتائج والتقييمات', en: '📊 View Results & Evaluations' },
  perm_view_recordings: { ar: '🎧 عرض/تحميل التسجيلات', en: '🎧 View/Download Recordings' },
  perm_generate_test_links: { ar: '🔗 توليد روابط اختبار', en: '🔗 Generate Test Links' },
  perm_make_calls: { ar: '📞 إجراء مكالمة (المندوب نفسه)', en: '📞 Make a Call (as trainee)' },

  // Users Management view
  users_mgmt_title: { ar: '👥 إدارة المستخدمين', en: '👥 User Management' },
  users_mgmt_back_btn: { ar: '⬅ رجوع للإعدادات', en: '⬅ Back to Settings' },
  user_form_title_add: { ar: '➕ إضافة مستخدم جديد', en: '➕ Add New User' },
  user_form_title_edit: { ar: '✏️ تعديل: {name}', en: '✏️ Edit: {name}' },
  user_name_label: { ar: 'الاسم *', en: 'Name *' },
  user_name_placeholder: { ar: 'مثال: محمد القحطاني', en: 'e.g. Mohamed Alqahtani' },
  user_username_label: { ar: 'اسم المستخدم (بريد إلكتروني) *', en: 'Username (email) *' },
  user_username_placeholder: { ar: 'مثال: m.alqahtani@izam.co', en: 'e.g. m.alqahtani@izam.co' },
  user_role_label: { ar: 'المسمى الوظيفي *', en: 'Job Title *' },
  user_choose_placeholder: { ar: '— اختر —', en: '— Choose —' },
  user_password_label: { ar: 'كلمة المرور', en: 'Password' },
  user_password_placeholder: { ar: 'فاضي = توليد تلقائي (إضافة) أو بدون تغيير (تعديل)', en: 'Empty = auto-generate (add) or no change (edit)' },
  user_status_label: { ar: 'الحالة', en: 'Status' },
  user_status_active: { ar: 'نشط', en: 'Active' },
  user_status_inactive: { ar: 'غير نشط', en: 'Inactive' },
  user_permissions_title: { ar: 'الصلاحيات', en: 'Permissions' },
  user_classifications_title: { ar: 'التصنيفات المسموحة', en: 'Allowed Classifications' },
  user_all_classifications: { ar: '🌐 كل التصنيفات', en: '🌐 All Classifications' },
  user_submit_add_btn: { ar: '➕ إضافة مستخدم', en: '➕ Add User' },
  user_submit_edit_btn: { ar: '💾 حفظ التعديلات', en: '💾 Save Changes' },
  user_cancel_edit_btn: { ar: 'إلغاء التعديل', en: 'Cancel Edit' },
  bulk_users_title: { ar: 'توليد حسابات جماعي (بدون صلاحيات — تُضبط لاحقًا من التعديل)', en: 'Bulk-generate accounts (no permissions — set later via edit)' },
  bulk_users_desc: { ar: 'اكتب الأسماء (كل اسم في سطر) وسننشئ لكل اسم حسابًا باسم مستخدم تلقائي وكلمة مرور عشوائية — تظهر هنا مرة واحدة فقط لتنقلها لمن يخصّها.', en: 'Enter names (one per line) and we will create an account for each with an auto-generated username and random password — shown here only once so you can pass it along.' },
  bulk_users_prefix_label: { ar: 'بادئة اسم المستخدم', en: 'Username prefix' },
  bulk_users_prefix_placeholder: { ar: 'مثال: sdr', en: 'e.g. sdr' },
  bulk_users_role_none: { ar: '— بدون —', en: '— None —' },
  bulk_users_names_label: { ar: 'الأسماء (كل اسم في سطر)', en: 'Names (one per line)' },
  bulk_users_generate_btn: { ar: '⚡ توليد الحسابات', en: '⚡ Generate Accounts' },
  users_mgmt_no_permissions: { ar: 'بدون صلاحيات', en: 'No permissions' },
  users_mgmt_all_classifications_scope: { ar: 'كل التصنيفات', en: 'All classifications' },
  users_mgmt_classifications_scope: { ar: '{count} تصنيف محدد', en: '{count} classification(s) selected' },
  users_mgmt_edit_btn: { ar: 'تعديل', en: 'Edit' },
  users_mgmt_delete_btn: { ar: 'حذف', en: 'Delete' },
  users_mgmt_delete_confirm: { ar: 'هل أنت متأكد من حذف المستخدم {username}؟', en: 'Are you sure you want to delete user {username}?' },
  users_mgmt_delete_failed: { ar: 'فشل حذف المستخدم: {error}', en: 'Failed to delete user: {error}' },
  users_mgmt_save_failed: { ar: 'تعذّر حفظ المستخدم: {error}', en: 'Failed to save user: {error}' },
  bulk_users_generate_failed: { ar: 'تعذّر توليد الحسابات: {error}', en: 'Failed to generate accounts: {error}' },
  bulk_table_name: { ar: 'الاسم', en: 'Name' },
  bulk_table_username: { ar: 'اسم المستخدم', en: 'Username' },
  bulk_table_password: { ar: 'كلمة المرور', en: 'Password' },
  bulk_table_copy_btn: { ar: 'نسخ', en: 'Copy' },
  bulk_table_copy_all_btn: { ar: 'نسخ الكل', en: 'Copy All' },
  copy_feedback_copied: { ar: '✅ اتنسخ', en: '✅ Copied' },
  copy_failed: { ar: 'تعذّر النسخ — انسخ يدويًا', en: 'Copy failed — copy manually' },
  status_deleting: { ar: 'جاري الحذف...', en: 'Deleting...' },
  status_creating: { ar: 'جاري الإنشاء...', en: 'Creating...' },
  status_generating: { ar: 'جاري التوليد...', en: 'Generating...' },
  link_gen_loading_overlay: { ar: 'جاري إنشاء رابط الاختبار...', en: 'Creating the test link...' },
  link_gen_create_failed: { ar: 'فشل إنشاء الرابط: {error}', en: 'Failed to create link: {error}' },

  // Results list
  result_default_scenario_name: { ar: 'مكالمة', en: 'Call' },
  result_duration_label: { ar: 'المدة: {seconds} ثانية', en: 'Duration: {seconds} sec' },
  result_customer_label: { ar: 'العميل: {name}', en: 'Customer: {name}' },
  result_view_details_btn: { ar: 'عرض التفاصيل', en: 'View Details' },
  result_hide_details_btn: { ar: 'إخفاء التفاصيل', en: 'Hide Details' },
  result_download_pdf_btn: { ar: '📄 تحميل PDF', en: '📄 Download PDF' },
  result_delete_btn: { ar: '🗑️ حذف', en: '🗑️ Delete' },
  result_strengths_title: { ar: 'نقاط القوة', en: 'Strengths' },
  result_improvements_title: { ar: 'نقاط التحسين', en: 'Areas for Improvement' },
  result_coaching_title: { ar: 'التوجيه', en: 'Coaching' },
  result_coaching_none: { ar: 'لا يوجد', en: 'None' },
  result_transcript_title: { ar: 'الحوار', en: 'Transcript' },
  transcript_role_trainee: { ar: 'المندوب', en: 'Trainee' },
  transcript_role_customer: { ar: 'العميل', en: 'Customer' },
  pdf_download_failed: { ar: 'فشل تحميل PDF: {error}', en: 'Failed to download PDF: {error}' },
  result_delete_confirm: { ar: 'هل أنت متأكد من حذف هذه المكالمة نهائيًا (النتيجة + التسجيل + PDF)؟ لا يمكن التراجع.', en: 'Are you sure you want to permanently delete this call (result + recording + PDF)? This cannot be undone.' },
  result_delete_failed: { ar: 'فشل حذف المكالمة: {error}', en: 'Failed to delete call: {error}' },
  results_bulk_delete_confirm: { ar: 'هل أنت متأكد من حذف {count} مكالمة نهائيًا (النتائج + التسجيلات + ملفات PDF)؟ لا يمكن التراجع.', en: 'Are you sure you want to permanently delete {count} call(s) (results + recordings + PDF files)? This cannot be undone.' },
  results_bulk_delete_failed: { ar: 'فشل حذف المكالمات المحددة: {error}', en: 'Failed to delete selected calls: {error}' },
  status_downloading: { ar: 'جاري التحميل...', en: 'Downloading...' },
};

function t(key, vars) {
  const lang = getLanguage();
  const entry = TRANSLATIONS[key];
  if (!entry) return key;
  let text = entry[lang] || entry.ar || key;
  if (vars) {
    Object.keys(vars).forEach((k) => { text = text.replaceAll(`{${k}}`, vars[k]); });
  }
  return text;
}

function applyLanguageAttrs() {
  const lang = getLanguage();
  document.documentElement.lang = lang;
  document.documentElement.dir = lang === 'en' ? 'ltr' : 'rtl';
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
      <div class="topbar-inner topbar-inner-center">
        <img class="logo" src="/Logo.png" alt="izam">
        <h1>${t('brand_name')}</h1>
      </div>
    </header>
    <main class="container">
      <div class="card login-card">
        <img class="login-card-logo" src="/Logo.png" alt="izam">
        <h2>${t('login_title')}</h2>
        <p class="muted">${t('login_subtitle')}</p>
        ${error ? `<div class="alert">${escapeHtml(error)}</div>` : ''}
        <form id="login-form" class="form-grid">
          <label>${t('login_email_label')}
            <input name="username" type="email" autocomplete="email" required placeholder="${t('login_email_placeholder')}" />
          </label>
          <label>${t('login_code_label')}
            <input name="password" type="password" autocomplete="off" required placeholder="${t('login_code_placeholder')}" />
          </label>
          <button type="submit" class="btn btn-primary full">${t('login_button')}</button>
        </form>
      </div>
    </main>`);

  document.getElementById('login-form').addEventListener('submit', (e) => {
    e.preventDefault();
    doLogin();
  });
}

async function doLogin() {
  const form = document.getElementById('login-form');
  const username = form.elements.username.value.trim();
  const password = form.elements.password.value;
  const submitBtn = form.querySelector('button[type="submit"]');
  setBtnLoading(submitBtn, true, 'جاري تسجيل الدخول...');
  showLoading('جاري تسجيل الدخول...');
  try {
    const res = await api.post('/api/login', { username, password });

    // Candidate whose test call already ended or is in progress: no token issued.
    if (res.status === 'ended') {
      renderLogin(res.message || 'You already completed your test call.');
      return;
    }
    if (res.status === 'started') {
      renderLogin(res.message || 'Test call in progress.');
      return;
    }

    // Matched the Candidates sheet -> candidate flow.
    if (res.candidate) {
      setCandidateToken(res.token);
      candidate = res.candidate;
      renderCandidateReady();
      return;
    }

    // Matched the Heads sheet -> internal flow.
    setToken(res.token);
    user = res.user;
    await loadRoleData();
    renderLanding();
  } catch (err) {
    let msg = err.message || 'Unknown error';
    if (msg.includes('401')) msg = 'Invalid email or access code';
    renderLogin(`Login failed: ${msg}`);
  } finally {
    hideLoading();
    setBtnLoading(submitBtn, false);
  }
}

function logout() {
  api.logout().catch(() => {});
  setToken('');
  setCandidateToken('');
  user = null;
  candidate = null;
  scenarios = [];
  users = [];
  renderLogin();
}

async function loadRoleData() {
  if (!isAdminLike(user)) {
    scenarios = [];
  } else {
    const sc = await api.getScenarios();
    scenarios = sc.scenarios || [];
  }
  if (isAdminLike(user)) {
    try {
      users = (await api.getUsers()).users || [];
    } catch (_) {
      users = [];
    }
    try {
      classifications = (await api.getClassifications()).classifications || [];
    } catch (_) {
      classifications = [];
    }
    try {
      aiClients = (await api.getAIClients()).ai_clients || [];
    } catch (_) {
      aiClients = [];
    }
    try {
      testSessions = (await api.getTestSessions()).test_sessions || [];
    } catch (_) {
      testSessions = [];
    }
    try {
      calls = (await api.getCallsList()).calls || [];
    } catch (_) {
      calls = [];
    }
  }
}

/* ---------------- الرئيسية (حسب الدور) ---------------- */

let currentView = 'dashboard'; // 'dashboard', 'classifications', 'ai-clients', 'test-sessions', 'results', 'settings'
let selectedClassification = null;
let selectedAIClient = null;
let testSessions = [];
let results = [];
let selectedResultRooms = new Set();

function renderAdminDashboard() {
  if (!user || !isAdminLike(user)) {
    renderLanding();
    return;
  }

  const perms = user.permissions || {};
  const navItems = [
    { id: 'dashboard', label: t('nav_dashboard'), perm: null },
    { id: 'classifications', label: t('nav_classifications'), perm: 'manage_classifications' },
    { id: 'ai-clients', label: t('nav_ai_clients'), perm: 'manage_ai_clients' },
    { id: 'calls', label: t('nav_calls'), perm: 'view_results' },
    { id: 'test-sessions', label: t('nav_test_sessions'), perm: 'view_results' },
    { id: 'results', label: t('nav_results'), perm: 'view_results' },
    { id: 'settings', label: t('nav_settings'), perm: null },
  ].filter(item => !item.perm || perms[item.perm]);

  // لو currentView صفحة المستخدم مش مسموحله يشوفها (صلاحياته اتغيرت مثلاً)، رجّعه للوحة التحكم
  const allowedViewIds = new Set(navItems.map(i => i.id));
  if (currentView === 'users-management' && !perms.manage_users) {
    currentView = 'dashboard';
  } else if (!allowedViewIds.has(currentView) && currentView !== 'users-management') {
    currentView = 'dashboard';
  }

  const navHTML = navItems.map(item => `
    <button class="nav-item ${currentView === item.id || (item.id === 'settings' && currentView === 'users-management') ? 'active' : ''}" data-view="${item.id}">
      ${item.label}
    </button>
  `).join('');

  let contentHTML = '';
  switch (currentView) {
    case 'dashboard':
      contentHTML = renderDashboardHome();
      break;
    case 'classifications':
      contentHTML = renderClassificationsView();
      break;
    case 'ai-clients':
      contentHTML = renderAIClientsView();
      break;
    case 'calls':
      contentHTML = renderCallsView();
      break;
    case 'test-sessions':
      contentHTML = renderTestSessionsView();
      break;
    case 'results':
      contentHTML = renderResultsView();
      break;
    case 'settings':
      contentHTML = renderSettingsView();
      break;
    case 'users-management':
      contentHTML = renderUsersManagementView();
      break;
  }

  render(`
    ${topbar(user.name || user.username)}
    <main class="admin-layout">
      <nav class="admin-sidebar">
        <div class="admin-logo">
          <img class="logo" src="/Logo.png" alt="izam">
          <h2>Daftra AI - Simulator</h2>
        </div>
        <div class="admin-nav">
          ${navHTML}
        </div>
        <div class="admin-nav-bottom">
          <button class="btn-logout" id="btn-logout">${t('nav_logout')}</button>
        </div>
      </nav>
      <div class="admin-content">
        ${contentHTML}
      </div>
    </main>
  `);

  // Add event listeners
  document.getElementById('btn-logout')?.addEventListener('click', logout);
  
  document.querySelectorAll('[data-view]').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      currentView = btn.dataset.view;
      renderAdminDashboard();
    });
  });

  // Initialize view-specific functionality
  if (currentView === 'classifications') initClassificationsView();
  if (currentView === 'ai-clients') initAIClientsView();
  if (currentView === 'calls') initCallsView();
  if (currentView === 'test-sessions') { initTestSessionsView(); loadTestSessions(); }
  if (currentView === 'results') { initResultsToolbar(); loadResults(); }
  if (currentView === 'settings') initSettingsView();
  if (currentView === 'users-management') initUsersManagementView();
}

function renderDashboardHome() {
  return `
    <div class="dashboard-header">
      <h1>${t('dashboard_title')}</h1>
      <p class="muted">${t('dashboard_welcome', { name: escapeHtml(user.name || user.username) })}</p>
    </div>
    <div class="dashboard-stats">
      <div class="stat-card">
        <div class="stat-icon">📂</div>
        <div class="stat-info">
          <h3>${classifications.length}</h3>
          <p>${t('stat_classifications')}</p>
        </div>
      </div>
      <div class="stat-card">
        <div class="stat-icon">🤖</div>
        <div class="stat-info">
          <h3>${aiClients.length}</h3>
          <p>${t('stat_ai_clients')}</p>
        </div>
      </div>
      <div class="stat-card">
        <div class="stat-icon">📞</div>
        <div class="stat-info">
          <h3>${testSessions.length}</h3>
          <p>${t('stat_test_sessions')}</p>
        </div>
      </div>
      <div class="stat-card">
        <div class="stat-icon">👥</div>
        <div class="stat-info">
          <h3>${users.length}</h3>
          <p>${t('stat_users')}</p>
        </div>
      </div>
    </div>
    <div class="dashboard-sections">
      <div class="dashboard-section" data-view="classifications">
        <h3>${t('home_card_classifications_title')}</h3>
        <p>${t('home_card_classifications_desc')}</p>
        <button class="btn btn-primary">${t('home_card_classifications_btn')}</button>
      </div>
      <div class="dashboard-section" data-view="test-sessions">
        <h3>${t('home_card_test_sessions_title')}</h3>
        <p>${t('home_card_test_sessions_desc')}</p>
        <button class="btn btn-primary">${t('home_card_test_sessions_btn')}</button>
      </div>
      <div class="dashboard-section" data-view="results">
        <h3>${t('home_card_results_title')}</h3>
        <p>${t('home_card_results_desc')}</p>
        <button class="btn btn-primary">${t('home_card_results_btn')}</button>
      </div>
    </div>
  `;
}

function renderClassificationsView() {
  return `
    <div class="view-header">
      <h1>${t('classifications_title')}</h1>
      <button class="btn btn-primary" id="btn-add-classification">${t('classifications_add_btn')}</button>
    </div>
    <div id="classifications-grid" class="classifications-grid">
      ${classifications.length === 0 ? `<p class="muted">${t('classifications_empty')}</p>` : ''}
    </div>
    <div id="classification-form-modal" class="modal" style="display:none;">
      <div class="modal-content">
        <h2 id="classification-form-title">${t('classification_form_title_add')}</h2>
        <form id="classification-form" class="form-grid">
          <input type="hidden" name="classification_id" value="" />
          <label>${t('classification_name_label')}
            <input name="name" placeholder="${t('classification_name_placeholder')}" required />
          </label>
          <label>${t('classification_description_label')}
            <input name="description" placeholder="${t('classification_description_placeholder')}" />
          </label>
          <label>${t('classification_min_duration_label')}
            <input name="min_duration_minutes" type="number" min="0" step="1" placeholder="${t('classification_optional_placeholder')}" />
          </label>
          <label>${t('classification_max_duration_label')}
            <input name="max_duration_minutes" type="number" min="0" step="1" placeholder="${t('classification_optional_placeholder')}" />
          </label>
          <label>${t('classification_default_difficulty_label')}
            <select name="default_difficulty">
              <option value="random" selected>${t('opt_random')}</option>
              <option value="easy">${t('opt_easy')}</option>
              <option value="medium">${t('opt_medium')}</option>
              <option value="hard">${t('opt_hard')}</option>
            </select>
          </label>
          <label>${t('classification_default_personality_label')}
            <select name="default_personality">
              <option value="random" selected>${t('opt_random')}</option>
              <option value="">${t('opt_by_difficulty')}</option>
              <option value="friendly">${t('opt_friendly')}</option>
              <option value="busy">${t('opt_busy')}</option>
              <option value="hesitant">${t('opt_hesitant')}</option>
              <option value="price_sensitive">${t('opt_price_sensitive')}</option>
              <option value="skeptical">${t('opt_skeptical')}</option>
              <option value="angry">${t('opt_angry')}</option>
              <option value="confused">${t('opt_confused')}</option>
              <option value="interested">${t('opt_interested')}</option>
              <option value="low_intent">${t('opt_low_intent')}</option>
              <option value="competitor">${t('opt_competitor')}</option>
              <option value="difficult">${t('opt_difficult')}</option>
              <option value="indecisive">${t('opt_indecisive')}</option>
            </select>
          </label>
          <label>${t('classification_default_knowledgeable_label')}
            <select name="default_knowledgeable">
              <option value="random" selected>${t('opt_random')}</option>
              <option value="true">${t('opt_knowledgeable_true')}</option>
              <option value="false">${t('opt_knowledgeable_false')}</option>
            </select>
          </label>
          <div class="form-actions">
            <button type="submit" class="btn btn-secondary">${t('save')}</button>
            <button type="button" class="btn btn-ghost" id="btn-cancel-classification">${t('cancel')}</button>
          </div>
        </form>
      </div>
    </div>
  `;
}

function renderAIClientsView() {
  const classificationOptions = classifications.map(c => 
    `<option value="${escapeHtml(c.classification_id)}">${escapeHtml(c.name)}</option>`
  ).join('');

  return `
    <div class="view-header">
      <h1>${t('ai_clients_title')}</h1>
      <div style="display:flex;gap:0.5rem;">
        <button class="btn btn-secondary" id="btn-generate-ai-clients">${t('ai_clients_generate_btn')}</button>
        <button class="btn btn-primary" id="btn-add-ai-client">${t('ai_clients_add_btn')}</button>
      </div>
    </div>
    <div class="filter-bar">
      <label>${t('ai_clients_filter_label')}
        <select id="ai-client-filter">
          <option value="">${t('ai_clients_filter_all')}</option>
          ${classificationOptions}
        </select>
      </label>
    </div>
    <div id="ai-clients-grid" class="ai-clients-grid">
      ${aiClients.length === 0 ? `<p class="muted">${t('ai_clients_empty')}</p>` : ''}
    </div>
    <div id="generate-ai-clients-modal" class="modal" style="display:none;">
      <div class="modal-content">
        <h2>${t('generate_modal_title')}</h2>
        <p class="muted">${t('generate_modal_desc')}</p>
        <form id="generate-ai-clients-form" class="form-grid">
          <label>${t('generate_count_label')}
            <input name="count" type="number" min="1" max="50" value="5" required />
          </label>
          <label>${t('generate_country_label')}
            <select name="country">
              <option value="both">${t('generate_country_both')}</option>
              <option value="eg">${t('generate_country_eg')}</option>
              <option value="sa">${t('generate_country_sa')}</option>
            </select>
          </label>
          <label>${t('generate_classification_label')}
            <select name="classification_id">
              <option value="">${t('generate_no_classification')}</option>
              ${classificationOptions}
            </select>
          </label>
          <div class="form-actions">
            <button type="submit" class="btn btn-secondary">${t('generate_submit_btn')}</button>
            <button type="button" class="btn btn-ghost" id="btn-cancel-generate-ai-clients">${t('cancel')}</button>
          </div>
        </form>
      </div>
    </div>
    <div id="ai-client-form-modal" class="modal" style="display:none;">
      <div class="modal-content modal-large">
        <h2 id="ai-client-form-title">${t('ai_client_form_title_add')}</h2>
        <form id="ai-client-form" class="form-grid">
          <input type="hidden" name="client_id" value="" />
          <label>${t('ai_client_name_label')}
            <input name="name" placeholder="${t('ai_client_name_placeholder')}" required />
          </label>
          <label>${t('ai_client_status_label')}
            <select name="active">
              <option value="true" selected>${t('ai_client_status_active')}</option>
              <option value="false">${t('ai_client_status_inactive')}</option>
            </select>
          </label>
          <label>${t('ai_client_language_label')}
            <select name="customer_language">
              <option value="ar" selected>🇸🇦 عربي</option>
              <option value="en">🇺🇸 English</option>
            </select>
          </label>
          <label>${t('ai_client_classification_label')}
            <select name="classification_id">${classificationOptions}</select>
          </label>
          <label>${t('ai_client_scenario_label')}
            <select name="scenario">
              <option value="new-lead-discovery-call">${t('scenario_discovery')}</option>
              <option value="follow-up-call">${t('scenario_followup')}</option>
              <option value="demo-call">${t('scenario_demo')}</option>
            </select>
          </label>
          <label>${t('ai_client_country_label')}
            <select name="country">
              <option value="sa">${t('country_sa')}</option>
              <option value="eg">${t('country_eg')}</option>
            </select>
          </label>
          <label>${t('ai_client_queue_label')}
            <select name="queue">
              <option value="">${t('ai_client_queue_none')}</option>
              <option value="Global SDR">Global SDR</option>
              <option value="EG SDR">EG SDR</option>
              <option value="Global Sales">Global Sales</option>
              <option value="EG Sales">EG Sales</option>
              <option value="KSA SDR">KSA SDR</option>
              <option value="KSA Sales">KSA Sales</option>
            </select>
          </label>
          <label>${t('ai_client_voice_label')}
            <select name="voice">
              <option value="">${t('ai_client_voice_auto')}</option>
              <option value="balRgnGuyobFvldHuixQ">Taba - سعودي</option>
              <option value="FELlZ7P5dpkH4BJsVcWt">Moazz - سعودي</option>
              <option value="SzEQh89cwBQTN77VF8m7">Salem Ahmed - سعودي</option>
              <option value="vhIzf2BLcnHOQbGSOOSe">GAWALY - سعودي</option>
              <option value="hlz7UHeM8xI2YLxXpNSY">Houzimi 2 - سعودي</option>
              <option value="EdfmSDZWwPKvZKdHoa4A">Azaam - مصري</option>
              <option value="M2ZmelgiD3eutyzRXWIZ">siso - مصري</option>
              <option value="6wEGFb9HfmAGibGevl4o">sisi - مصري</option>
              <option value="zxJ1nKbTdTEDKgscIQ5T">fla7 - مصري</option>
              <option value="vwggHPV0xUSj5rWYC6Qo">Omar Osama</option>
              <option value="DZ6l6I8upRav2eGarvdV">Hessin Abdallah</option>
              <option value="z5ZE8UcKJIRdZ8L0mw2R">meshary</option>
            </select>
          </label>
          <label>${t('ai_client_difficulty_label')}
            <select name="difficulty">
              <option value="random" selected>${t('opt_random')}</option>
              <option value="easy">${t('opt_easy')}</option>
              <option value="medium">${t('opt_medium')}</option>
              <option value="hard">${t('opt_hard')}</option>
            </select>
          </label>
          <label>${t('ai_client_customer_name_label')}
            <input name="customer_name" placeholder="${t('ai_client_customer_name_placeholder')}" />
          </label>
          <label>${t('ai_client_customer_role_label')}
            <input name="customer_role" placeholder="${t('ai_client_customer_role_placeholder')}" />
          </label>
          <label>${t('ai_client_company_name_label')}
            <input name="company_name" placeholder="${t('ai_client_company_name_placeholder')}" />
          </label>
          <label>${t('ai_client_business_field_label')}
            <input name="business_field" placeholder="${t('ai_client_business_field_placeholder')}" />
          </label>
          <label>${t('ai_client_company_size_label')}
            <input name="company_size" placeholder="${t('ai_client_company_size_placeholder')}" />
          </label>
          <label>${t('ai_client_pain_point_label')}
            <input name="pain_point" placeholder="${t('ai_client_pain_point_placeholder')}" />
          </label>
          <label>${t('ai_client_decision_maker_label')}
            <select name="decision_maker">
              <option value="true">${t('ai_client_decision_maker_yes')}</option>
              <option value="false">${t('ai_client_decision_maker_no')}</option>
            </select>
          </label>
          <label>${t('ai_client_personality_label')}
            <select name="personality">
              <option value="random">${t('opt_random')}</option>
              <option value="">${t('opt_by_difficulty')}</option>
              <option value="friendly">${t('opt_friendly')}</option>
              <option value="busy">${t('opt_busy')}</option>
              <option value="hesitant">${t('opt_hesitant')}</option>
              <option value="price_sensitive">${t('opt_price_sensitive')}</option>
              <option value="skeptical">${t('opt_skeptical')}</option>
              <option value="angry">${t('opt_angry')}</option>
              <option value="confused">${t('opt_confused')}</option>
              <option value="interested">${t('opt_interested')}</option>
              <option value="low_intent">${t('opt_low_intent')}</option>
              <option value="competitor">${t('opt_competitor')}</option>
              <option value="difficult">${t('opt_difficult')}</option>
              <option value="indecisive">${t('opt_indecisive')}</option>
            </select>
          </label>
          <label>${t('ai_client_temperature_label')}
            <select name="temperature">
              <option value="warm">${t('ai_client_temperature_warm')}</option>
              <option value="cold">${t('ai_client_temperature_cold')}</option>
            </select>
          </label>
          <label>${t('ai_client_budget_sensitivity_label')}
            <select name="budget_sensitivity">
              <option value="منخفضة">${t('level_low')}</option>
              <option value="متوسطة" selected>${t('level_medium')}</option>
              <option value="عالية">${t('level_high')}</option>
            </select>
          </label>
          <label>${t('ai_client_buying_intent_label')}
            <select name="buying_intent">
              <option value="منخفضة">${t('level_low')}</option>
              <option value="متوسطة" selected>${t('level_medium')}</option>
              <option value="عالية">${t('level_high')}</option>
            </select>
          </label>
          <label>${t('ai_client_knowledgeable_label')}
            <select name="knowledgeable">
              <option value="random" selected>${t('opt_random')}</option>
              <option value="true">${t('opt_knowledgeable_true')}</option>
              <option value="false">${t('opt_knowledgeable_false')}</option>
            </select>
          </label>
          <label class="full">${t('ai_client_instructions_label')}
            <textarea name="instructions" rows="2" placeholder="${t('ai_client_instructions_placeholder')}"></textarea>
          </label>
          <label class="full">${t('ai_client_objectives_label')}
            <textarea name="objectives" rows="2" placeholder="${t('ai_client_objectives_placeholder')}"></textarea>
          </label>
          <label class="full">${t('ai_client_objections_label')}
            <textarea name="objections" rows="2" placeholder="${t('ai_client_objections_placeholder')}"></textarea>
          </label>
          <label class="full">${t('ai_client_product_brief_label')}
            <textarea name="product_brief" rows="2" placeholder="${t('ai_client_product_brief_placeholder')}"></textarea>
          </label>
          <div class="form-actions">
            <button type="submit" class="btn btn-secondary">${t('ai_client_save_btn')}</button>
            <button type="button" class="btn btn-ghost" id="btn-cancel-ai-client">${t('cancel')}</button>
          </div>
        </form>
      </div>
    </div>
  `;
}

function renderCallsView() {
  return `
    <div class="view-header">
      <h1>${t('calls_title')}</h1>
      <div class="filter-group">
        <select id="calls-classification-filter" class="form-control">
          <option value="">${t('calls_all_classifications')}</option>
          ${classifications.map(c => `<option value="${escapeHtml(c.classification_id)}">${escapeHtml(c.name)}</option>`).join('')}
        </select>
      </div>
    </div>
    <div class="results-toolbar" id="calls-toolbar" style="display:flex;align-items:center;gap:0.75rem;margin-bottom:0.75rem;">
      <label style="display:flex;align-items:center;gap:0.4rem;">
        <input type="checkbox" id="calls-select-all" />
        ${t('results_select_all')}
      </label>
      <button class="btn-danger-ghost" id="btn-delete-selected-calls" disabled>${t('results_delete_selected_btn', { count: 0 })}</button>
    </div>
    <div id="calls-list" class="calls-list">
      <p class="muted">${t('calls_loading')}</p>
    </div>
  `;
}

function renderTestSessionsView() {
  return `
    <div class="view-header">
      <h1>${t('test_sessions_title')}</h1>
      <button class="btn btn-primary" id="btn-create-link">${t('test_sessions_create_link_btn')}</button>
    </div>
    <div class="card" id="link-settings-section" style="margin-bottom:1rem;">
      <h3>${t('link_settings_title')}</h3>
      <p class="muted">${t('link_settings_desc')}</p>
      <div style="display:flex;gap:0.5rem;align-items:flex-end;">
        <label style="flex:0 0 auto;">${t('link_settings_max_label')}
          <input id="max-reschedule-input" type="number" min="-1" step="1" style="width:100px;" />
        </label>
        <button class="btn btn-secondary" id="btn-save-link-settings">${t('save')}</button>
      </div>
      <p class="muted small" id="link-settings-status" style="margin-top:8px;"></p>
    </div>
    <div id="test-sessions-list" class="test-sessions-list">
      ${testSessions.length === 0 ? `<p class="muted">${t('test_sessions_empty')}</p>` : ''}
    </div>
    <div id="link-gen-modal" class="modal" style="display:none;">
      <div class="modal-content">
        <h2>${t('link_gen_modal_title')}</h2>
        <form id="link-gen-form" class="form-grid">
          <label>${t('link_gen_candidate_name_label')}
            <input name="candidate_name" placeholder="${t('link_gen_candidate_name_placeholder')}" required />
          </label>
          <label>${t('link_gen_candidate_email_label')}
            <input name="candidate_email" type="email" placeholder="${t('link_gen_candidate_email_placeholder')}" required />
          </label>
          <label>${t('link_gen_candidate_id_label')}
            <input name="candidate_id" placeholder="${t('link_gen_candidate_id_placeholder')}" />
          </label>
          <label>${t('link_gen_classification_label')}
            <select name="classification_id">
              <option value="">${t('link_gen_classification_placeholder')}</option>
              ${classifications.map(c => `<option value="${escapeHtml(c.classification_id)}">${escapeHtml(c.name)}</option>`).join('')}
            </select>
          </label>
          <label>${t('link_gen_ai_client_label')}
            <select name="ai_client_id">
              <option value="">${t('link_gen_ai_client_placeholder')}</option>
            </select>
          </label>
          <div class="form-actions">
            <button type="submit" class="btn btn-secondary">${t('link_gen_submit_btn')}</button>
            <button type="button" class="btn btn-ghost" id="btn-cancel-link">${t('cancel')}</button>
          </div>
        </form>
        <div id="generated-link" style="margin-top:1rem;display:none;">
          <label>${t('link_gen_result_label')}
            <input id="generated-link-input" readonly style="direction:ltr;font-family:monospace;" />
          </label>
          <button class="btn btn-ghost" id="btn-copy-link" style="margin-top:0.5rem;">${t('link_gen_copy_btn')}</button>
        </div>
      </div>
    </div>
  `;
}

function renderResultsView() {
  return `
    <div class="view-header">
      <h1>${t('results_title')}</h1>
    </div>
    <div class="results-toolbar" id="results-toolbar" style="display:flex;align-items:center;gap:0.75rem;margin-bottom:0.75rem;">
      <label style="display:flex;align-items:center;gap:0.4rem;">
        <input type="checkbox" id="results-select-all" />
        ${t('results_select_all')}
      </label>
      <button class="btn-danger-ghost" id="btn-delete-selected-results" disabled>${t('results_delete_selected_btn', { count: 0 })}</button>
    </div>
    <div id="results-list" class="results-list">
      ${results.length === 0 ? `<p class="muted">${t('results_empty')}</p>` : ''}
    </div>
  `;
}

let pendingSettings = {};

function renderSettingsView() {
  const savedLang = localStorage.getItem('daftraai_language') || 'ar';
  const currentLang = pendingSettings.language ?? savedLang;
  const isDirty = 'language' in pendingSettings && pendingSettings.language !== savedLang;
  return `
    <div class="view-header">
      <h1>${t('settings_title')}</h1>
    </div>
    <div class="settings-sections">
      <div class="settings-section">
        <h3>${t('settings_language_title')}</h3>
        <p class="muted">${t('settings_language_desc')}</p>
        <div class="language-selector">
          <button class="btn ${currentLang === 'ar' ? 'btn-primary' : 'btn-ghost'}" id="btn-lang-ar" type="button">🇸🇦 العربية</button>
          <button class="btn ${currentLang === 'en' ? 'btn-primary' : 'btn-ghost'}" id="btn-lang-en" type="button">🇺🇸 English</button>
        </div>
        <button class="btn btn-secondary" id="btn-save-settings" style="margin-top:12px;" ${isDirty ? '' : 'disabled'}>${t('settings_save')}</button>
        <p class="muted small" id="lang-status" style="margin-top:8px;">${isDirty ? t('settings_dirty') : ''}</p>
      </div>
      <div class="settings-section">
        <h3>${t('settings_users_title')}</h3>
        <p class="muted">${t('settings_users_desc')}</p>
        <button class="btn btn-primary" id="btn-manage-users">${t('settings_users_manage_btn')}</button>
      </div>
      <div class="settings-section">
        <h3>${t('settings_links_title')}</h3>
        <p class="muted">${t('settings_links_desc')}</p>
        <button class="btn btn-primary" id="btn-manage-links">${t('settings_links_manage_btn')}</button>
      </div>
      <div class="settings-section">
        <h3>${t('settings_diagnostic_title')}</h3>
        <p class="muted">${t('settings_diagnostic_desc')}</p>
        <button class="btn btn-primary" id="btn-diagnostic">${t('settings_diagnostic_run_btn')}</button>
      </div>
      <div class="settings-section">
        <h3>${t('settings_knowledge_title')}</h3>
        <p class="muted">${t('settings_knowledge_desc')}</p>
        <textarea id="knowledge-base-text" rows="12" style="width:100%;font-family:inherit;padding:8px;" placeholder="${t('settings_knowledge_placeholder')}"></textarea>
        <button class="btn btn-secondary" id="btn-save-knowledge" style="margin-top:8px;">${t('settings_knowledge_save_btn')}</button>
        <p class="muted small" id="knowledge-status" style="margin-top:8px;"></p>
      </div>
    </div>
  `;
}

function initSettingsView() {
  document.getElementById('btn-lang-ar')?.addEventListener('click', () => {
    pendingSettings.language = 'ar';
    renderAdminDashboard();
  });

  document.getElementById('btn-lang-en')?.addEventListener('click', () => {
    pendingSettings.language = 'en';
    renderAdminDashboard();
  });

  document.getElementById('btn-save-settings')?.addEventListener('click', () => {
    const languageChanged = 'language' in pendingSettings && pendingSettings.language !== getLanguage();
    if ('language' in pendingSettings) {
      localStorage.setItem('daftraai_language', pendingSettings.language);
    }
    pendingSettings = {};
    if (languageChanged) {
      // Full reload so <html dir/lang> and every screen re-render consistently
      // in the new language, instead of trying to re-render just the current view.
      location.reload();
      return;
    }
    renderAdminDashboard();
    const status = document.getElementById('lang-status');
    if (status) {
      status.textContent = t('settings_saved');
      setTimeout(() => { if (status.isConnected) status.textContent = ''; }, 2000);
    }
  });

  document.getElementById('btn-manage-users')?.addEventListener('click', () => {
    currentView = 'users-management';
    renderAdminDashboard();
  });

  document.getElementById('btn-manage-links')?.addEventListener('click', () => {
    currentView = 'test-sessions';
    renderAdminDashboard();
  });

  document.getElementById('btn-diagnostic')?.addEventListener('click', async () => {
    const btn = document.getElementById('btn-diagnostic');
    setBtnLoading(btn, true, t('status_checking'));
    try {
      const res = await api.getDiagnostic();
      alert(JSON.stringify(res, null, 2));
    } catch (err) {
      alert(t('diagnostic_failed', { error: err.message }));
    } finally {
      setBtnLoading(btn, false);
    }
  });

  const knowledgeTextarea = document.getElementById('knowledge-base-text');
  if (knowledgeTextarea) {
    api.getKnowledgeBase()
      .then((res) => { knowledgeTextarea.value = res.content || ''; })
      .catch(() => { knowledgeTextarea.placeholder = t('knowledge_load_failed'); });
  }

  document.getElementById('btn-save-knowledge')?.addEventListener('click', async () => {
    const btn = document.getElementById('btn-save-knowledge');
    setBtnLoading(btn, true, t('status_saving'));
    try {
      await api.saveKnowledgeBase(knowledgeTextarea.value);
      const status = document.getElementById('knowledge-status');
      if (status) {
        status.textContent = t('knowledge_saved');
        setTimeout(() => { if (status.isConnected) status.textContent = ''; }, 2000);
      }
    } catch (err) {
      alert(t('knowledge_save_failed', { error: err.message }));
    } finally {
      setBtnLoading(btn, false);
    }
  });
}

const PERMISSION_KEYS = [
  'manage_users', 'manage_classifications', 'manage_ai_clients', 'delete_calls_results',
  'view_results', 'view_recordings', 'generate_test_links', 'make_calls',
];

function permissionLabel(key) {
  return t('perm_' + key) !== ('perm_' + key) ? t('perm_' + key) : key;
}

let userMeta = { roles: [], permission_keys: PERMISSION_KEYS };
let editingUsername = '';

function renderUsersManagementView() {
  return `
    <div class="view-header">
      <h1>${t('users_mgmt_title')}</h1>
      <button class="btn btn-ghost btn-inline" id="btn-back-to-settings">${t('users_mgmt_back_btn')}</button>
    </div>
    <section class="card custom-card">
      <div class="users-list" id="users-mgmt-list"></div>

      <h3 class="section-title" id="user-form-title">${t('user_form_title_add')}</h3>
      <form id="users-form" class="form-grid">
        <input type="hidden" name="original_username" value="" />
        <label>${t('user_name_label')}
          <input name="name" placeholder="${t('user_name_placeholder')}" required />
        </label>
        <label>${t('user_username_label')}
          <input name="username" type="email" placeholder="${t('user_username_placeholder')}" required />
        </label>
        <label>${t('user_role_label')}
          <select name="role" id="user-role-select" required>
            <option value="">${t('user_choose_placeholder')}</option>
          </select>
        </label>
        <label>${t('user_password_label')}
          <input name="password" type="password" placeholder="${t('user_password_placeholder')}" />
        </label>
        <label>${t('user_status_label')}
          <select name="status">
            <option value="active">${t('user_status_active')}</option>
            <option value="inactive">${t('user_status_inactive')}</option>
          </select>
        </label>

        <div class="span2">
          <h4>${t('user_permissions_title')}</h4>
          <div id="permission-checkboxes" class="checkbox-grid"></div>
        </div>

        <div class="span2">
          <h4>${t('user_classifications_title')}</h4>
          <label style="display:flex;align-items:center;gap:0.4rem;">
            <input type="checkbox" id="all-classifications-cb" />
            ${t('user_all_classifications')}
          </label>
          <div id="classification-checkboxes" class="checkbox-grid"></div>
        </div>

        <div class="form-actions">
          <button type="submit" class="btn btn-secondary" id="btn-submit-user">${t('user_submit_add_btn')}</button>
          <button type="button" class="btn btn-ghost" id="btn-cancel-edit-user" style="display:none;">${t('user_cancel_edit_btn')}</button>
        </div>
      </form>

      <hr class="divider" />
      <h3 class="section-title">${t('bulk_users_title')}</h3>
      <p class="muted">${t('bulk_users_desc')}</p>
      <form id="bulk-form" class="form-grid">
        <label>${t('bulk_users_prefix_label')}
          <input name="base_username" value="sdr" placeholder="${t('bulk_users_prefix_placeholder')}" required />
        </label>
        <label>${t('user_role_label')}
          <select name="role" id="bulk-role-select">
            <option value="">${t('bulk_users_role_none')}</option>
          </select>
        </label>
        <label class="span2">${t('bulk_users_names_label')}
          <textarea name="names" rows="6" placeholder="محمد القحطاني&#10;سارة العتيبي&#10;خالد الشمري" required></textarea>
        </label>
        <button type="submit" class="btn btn-secondary full">${t('bulk_users_generate_btn')}</button>
      </form>
      <div id="bulk-users-result"></div>
    </section>
  `;
}

function populateRoleSelects() {
  const roleOptions = (userMeta.roles || []).map(r => `<option value="${escapeHtml(r)}">${escapeHtml(roleLabel(r))}</option>`).join('');
  const mainSelect = document.getElementById('user-role-select');
  if (mainSelect) mainSelect.innerHTML = `<option value="">${t('user_choose_placeholder')}</option>` + roleOptions;
  const bulkSelect = document.getElementById('bulk-role-select');
  if (bulkSelect) bulkSelect.innerHTML = `<option value="">${t('bulk_users_role_none')}</option>` + roleOptions;
}

function renderPermissionCheckboxes(selected = {}) {
  const container = document.getElementById('permission-checkboxes');
  if (!container) return;
  container.innerHTML = (userMeta.permission_keys || []).map(key => `
    <label style="display:flex;align-items:center;gap:0.4rem;">
      <input type="checkbox" data-perm="${escapeHtml(key)}" ${selected[key] ? 'checked' : ''} />
      ${escapeHtml(permissionLabel(key))}
    </label>
  `).join('');
}

function renderClassificationCheckboxes(selectedIds = [], allSelected = false) {
  const container = document.getElementById('classification-checkboxes');
  if (!container) return;
  container.innerHTML = classifications.map(c => `
    <label style="display:flex;align-items:center;gap:0.4rem;">
      <input type="checkbox" data-cls="${escapeHtml(c.classification_id)}" ${selectedIds.includes(c.classification_id) ? 'checked' : ''} ${allSelected ? 'disabled' : ''} />
      ${escapeHtml(c.name)}
    </label>
  `).join('');
}

function collectPermissionsFromForm() {
  const perms = {};
  document.querySelectorAll('#permission-checkboxes [data-perm]').forEach(cb => {
    perms[cb.dataset.perm] = cb.checked;
  });
  return perms;
}

function collectClassificationIdsFromForm() {
  const ids = [];
  document.querySelectorAll('#classification-checkboxes [data-cls]').forEach(cb => {
    if (cb.checked) ids.push(cb.dataset.cls);
  });
  return ids;
}

function startEditUser(u) {
  editingUsername = u.username;
  const form = document.getElementById('users-form');
  form.elements.original_username.value = u.username;
  form.elements.name.value = u.name || '';
  form.elements.username.value = u.username || '';
  form.elements.role.value = u.role || '';
  form.elements.password.value = '';
  form.elements.status.value = u.status === 'inactive' ? 'inactive' : 'active';
  renderPermissionCheckboxes(u.permissions || {});
  const allCb = document.getElementById('all-classifications-cb');
  allCb.checked = !!u.all_classifications;
  renderClassificationCheckboxes(u.classification_ids || [], !!u.all_classifications);
  document.getElementById('user-form-title').textContent = t('user_form_title_edit', { name: u.name || u.username });
  document.getElementById('btn-submit-user').textContent = t('user_submit_edit_btn');
  document.getElementById('btn-cancel-edit-user').style.display = '';
  form.scrollIntoView({ behavior: 'smooth' });
}

function resetUserForm() {
  editingUsername = '';
  const form = document.getElementById('users-form');
  if (!form) return;
  form.reset();
  form.elements.original_username.value = '';
  renderPermissionCheckboxes({});
  const allCb = document.getElementById('all-classifications-cb');
  if (allCb) allCb.checked = false;
  renderClassificationCheckboxes([], false);
  document.getElementById('user-form-title').textContent = t('user_form_title_add');
  document.getElementById('btn-submit-user').textContent = t('user_submit_add_btn');
  document.getElementById('btn-cancel-edit-user').style.display = 'none';
}

function renderUsersManagementList() {
  const container = document.getElementById('users-mgmt-list');
  if (!container) return;
  container.innerHTML = users.map(u => {
    const grantedPerms = Object.entries(u.permissions || {}).filter(([, v]) => v).map(([k]) => permissionLabel(k));
    const classScope = u.all_classifications ? t('users_mgmt_all_classifications_scope') : t('users_mgmt_classifications_scope', { count: (u.classification_ids || []).length });
    return `
    <div class="user-row">
      <span class="user-name">${escapeHtml(u.name || u.username)}</span>
      <span class="muted small">@${escapeHtml(u.username)}</span>
      <span class="user-role">${escapeHtml(u.role ? roleLabel(u.role) : '—')}</span>
      <span class="muted small">${escapeHtml(grantedPerms.join(getLanguage() === 'en' ? ', ' : '، ') || t('users_mgmt_no_permissions'))} · ${escapeHtml(classScope)}</span>
      <button class="btn btn-ghost btn-inline" data-action="edit-user" data-username="${escapeHtml(u.username)}">${t('users_mgmt_edit_btn')}</button>
      <button class="btn-danger-ghost" data-action="del-user" data-username="${escapeHtml(u.username)}">${t('users_mgmt_delete_btn')}</button>
    </div>`;
  }).join('');

  container.querySelectorAll('[data-action="edit-user"]').forEach(btn => {
    btn.addEventListener('click', () => {
      const u = users.find(x => x.username === btn.dataset.username);
      if (u) startEditUser(u);
    });
  });

  container.querySelectorAll('[data-action="del-user"]').forEach(btn => {
    btn.addEventListener('click', async () => {
      if (!confirm(t('users_mgmt_delete_confirm', { username: btn.dataset.username }))) return;
      setBtnLoading(btn, true, t('status_deleting'));
      try {
        await api.deleteUser(btn.dataset.username);
        users = (await api.getUsers()).users || [];
        renderUsersManagementList();
      } catch (err) {
        alert(t('users_mgmt_delete_failed', { error: err.message }));
      } finally {
        setBtnLoading(btn, false);
      }
    });
  });
}

function renderBulkUsersResult() {
  const container = document.getElementById('bulk-users-result');
  if (!container) return;
  if (!bulkResult || !bulkResult.length) {
    container.innerHTML = '';
    return;
  }
  container.innerHTML = `
    <table class="bulk-table">
      <thead><tr><th>${t('bulk_table_name')}</th><th>${t('bulk_table_username')}</th><th>${t('bulk_table_password')}</th><th></th></tr></thead>
      <tbody>
        ${bulkResult.map(c => `
          <tr>
            <td>${escapeHtml(c.name)}</td>
            <td>${escapeHtml(c.username)}</td>
            <td>${escapeHtml(c.password)}</td>
            <td><button class="btn btn-ghost btn-inline" data-action="copy-creds" data-creds="${escapeHtml(`${c.name} | ${c.username} | ${c.password}`)}">${t('bulk_table_copy_btn')}</button></td>
          </tr>`).join('')}
      </tbody>
    </table>
    <button class="btn btn-ghost" id="btn-copy-all-mgmt" type="button">${t('bulk_table_copy_all_btn')}</button>
  `;
  container.querySelectorAll('[data-action="copy-creds"]').forEach(btn => {
    btn.addEventListener('click', () => copyTextWithFeedback(btn.dataset.creds, btn));
  });
  document.getElementById('btn-copy-all-mgmt')?.addEventListener('click', (e) => {
    const text = bulkResult.map(c => `${c.name} | ${c.username} | ${c.password}`).join('\n');
    copyTextWithFeedback(text, e.currentTarget);
  });
}

function copyTextWithFeedback(text, btn) {
  navigator.clipboard?.writeText(text).then(() => {
    if (!btn) return;
    const original = btn.textContent;
    btn.textContent = t('copy_feedback_copied');
    setTimeout(() => { if (btn.isConnected) btn.textContent = original; }, 1500);
  }).catch(() => alert(t('copy_failed')));
}

async function initUsersManagementView() {
  renderUsersManagementList();
  renderBulkUsersResult();
  resetUserForm();

  try {
    userMeta = await api.getUserMeta();
  } catch (_) {
    // نسيب القيمة الافتراضية (مفاتيح الصلاحيات معروفة محليًا، الأدوار بس هتفضل فاضية)
  }
  populateRoleSelects();
  renderPermissionCheckboxes({});
  renderClassificationCheckboxes([], false);

  document.getElementById('btn-back-to-settings')?.addEventListener('click', () => {
    currentView = 'settings';
    renderAdminDashboard();
  });

  document.getElementById('btn-cancel-edit-user')?.addEventListener('click', () => resetUserForm());

  document.getElementById('all-classifications-cb')?.addEventListener('change', (e) => {
    document.querySelectorAll('#classification-checkboxes [data-cls]').forEach(cb => {
      cb.disabled = e.target.checked;
    });
  });

  document.getElementById('users-form')?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const form = e.target;
    const data = {
      username: form.elements.username.value.trim(),
      name: form.elements.name.value.trim(),
      role: form.elements.role.value,
      password: form.elements.password.value,
      status: form.elements.status.value,
      permissions: collectPermissionsFromForm(),
      all_classifications: document.getElementById('all-classifications-cb').checked,
      classification_ids: collectClassificationIdsFromForm(),
    };
    const btn = document.getElementById('btn-submit-user');
    setBtnLoading(btn, true, t('status_saving'));
    try {
      if (editingUsername) {
        await api.updateUser(editingUsername, data);
      } else {
        await api.createUser(data);
      }
      users = (await api.getUsers()).users || [];
      resetUserForm();
      renderUsersManagementList();
    } catch (err) {
      alert(t('users_mgmt_save_failed', { error: err.message }));
    } finally {
      setBtnLoading(btn, false);
    }
  });

  document.getElementById('bulk-form')?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const form = e.target;
    const names = (form.elements.names.value || '').split('\n').map(s => s.trim()).filter(Boolean);
    const base = form.elements.base_username.value.trim();
    const role = form.elements.role.value;
    const btn = form.querySelector('button[type="submit"]');
    setBtnLoading(btn, true, t('status_creating'));
    try {
      const res = await api.bulkUsers(names, base, role);
      bulkResult = res.users || [];
      users = (await api.getUsers()).users || [];
      form.reset();
      renderUsersManagementList();
      renderBulkUsersResult();
    } catch (err) {
      alert(t('bulk_users_generate_failed', { error: err.message }));
    } finally {
      setBtnLoading(btn, false);
    }
  });
}

function initClassificationsView() {
  renderAdminClassificationsList();

  document.getElementById('btn-add-classification')?.addEventListener('click', () => {
    document.getElementById('classification-form-modal').style.display = 'flex';
    document.getElementById('classification-form-title').textContent = t('classification_form_title_add');
    document.getElementById('classification-form').reset();
  });

  document.getElementById('btn-cancel-classification')?.addEventListener('click', () => {
    document.getElementById('classification-form-modal').style.display = 'none';
  });

  document.getElementById('classification-form')?.addEventListener('submit', (e) => {
    e.preventDefault();
    submitClassification();
  });
}

function renderAdminClassificationsList() {
  const container = document.getElementById('classifications-grid');
  if (!container) return;
  
  container.innerHTML = classifications.map(c => `
    <div class="classification-card" data-id="${escapeHtml(c.classification_id)}">
      <div class="card-header">
        <h3>${escapeHtml(c.name)}</h3>
        <span class="badge">${aiClients.filter(ac => ac.classification_id === c.classification_id).length} ${t('classification_clients_count')}</span>
      </div>
      <p class="muted">${escapeHtml(c.description || t('classification_no_description'))}</p>
      <div class="card-actions">
        <button class="btn btn-ghost" data-action="view-ai-clients" data-id="${escapeHtml(c.classification_id)}">${t('classification_view_clients_btn')}</button>
        <button class="btn btn-ghost" data-action="edit-classification" data-id="${escapeHtml(c.classification_id)}">${t('classification_edit_btn')}</button>
        <button class="btn btn-ghost" data-action="reset-classification-ai-clients" data-id="${escapeHtml(c.classification_id)}" title="${t('classification_reset_title')}">${t('classification_reset_btn')}</button>
        <button class="btn-danger-ghost" data-action="delete-classification" data-id="${escapeHtml(c.classification_id)}">${t('classification_delete_btn')}</button>
      </div>
    </div>
  `).join('');

  container.querySelectorAll('[data-action="view-ai-clients"]').forEach(btn => {
    btn.addEventListener('click', () => {
      selectedClassification = btn.dataset.id;
      currentView = 'ai-clients';
      renderAdminDashboard();
    });
  });

  container.querySelectorAll('[data-action="edit-classification"]').forEach(btn => {
    btn.addEventListener('click', () => {
      const c = classifications.find(x => x.classification_id === btn.dataset.id);
      if (!c) return;
      document.getElementById('classification-form-modal').style.display = 'flex';
      document.getElementById('classification-form-title').textContent = t('classification_form_title_edit');
      const form = document.getElementById('classification-form');
      form.querySelector('[name="classification_id"]').value = c.classification_id;
      form.querySelector('[name="name"]').value = c.name;
      form.querySelector('[name="description"]').value = c.description || '';
      form.querySelector('[name="min_duration_minutes"]').value = c.min_duration_minutes || '';
      form.querySelector('[name="max_duration_minutes"]').value = c.max_duration_minutes || '';
      form.querySelector('[name="default_difficulty"]').value = c.default_difficulty || 'random';
      form.querySelector('[name="default_personality"]').value = c.default_personality || 'random';
      form.querySelector('[name="default_knowledgeable"]').value = c.default_knowledgeable || 'random';
    });
  });

  container.querySelectorAll('[data-action="reset-classification-ai-clients"]').forEach(btn => {
    btn.addEventListener('click', async () => {
      if (!confirm(t('classification_reset_confirm'))) return;
      setBtnLoading(btn, true, t('status_resetting'));
      try {
        const res = await api.resetClassificationAIClients(btn.dataset.id);
        alert(t('classification_reset_success', { count: res.updated_count }));
        await loadAIClients();
      } catch (err) {
        alert(t('classification_reset_failed', { error: err.message }));
      } finally {
        setBtnLoading(btn, false);
      }
    });
  });

  container.querySelectorAll('[data-action="delete-classification"]').forEach(btn => {
    btn.addEventListener('click', async () => {
      if (!confirm(t('classification_delete_confirm'))) return;
      setBtnLoading(btn, true, t('status_deleting'));
      try {
        await api.deleteClassification(btn.dataset.id);
        await loadClassifications();
        renderAdminDashboard();
      } catch (err) {
        alert(t('classification_delete_failed', { error: err.message }));
      } finally {
        setBtnLoading(btn, false);
      }
    });
  });
}

function initAIClientsView() {
  loadAIClients(selectedClassification || '');
  
  document.getElementById('btn-add-ai-client')?.addEventListener('click', () => {
    document.getElementById('ai-client-form-modal').style.display = 'flex';
    document.getElementById('ai-client-form-title').textContent = t('ai_client_form_title_add');
    document.getElementById('ai-client-form').reset();
  });

  document.getElementById('btn-cancel-ai-client')?.addEventListener('click', () => {
    document.getElementById('ai-client-form-modal').style.display = 'none';
  });

  // اختيار تصنيف للعميل الذكي بيطبّق افتراضياته (صعوبة/شخصية/مدى معلومات) —
  // القيم دي لسه تقدر تغيّرها يدويًا قبل الحفظ.
  document.getElementById('ai-client-classification-select')?.addEventListener('change', (e) => {
    const cls = classifications.find(c => c.classification_id === e.target.value);
    if (!cls) return;
    const form = document.getElementById('ai-client-form');
    if (form.elements.difficulty) form.elements.difficulty.value = cls.default_difficulty || 'random';
    if (form.elements.personality) form.elements.personality.value = cls.default_personality || 'random';
    if (form.elements.knowledgeable) form.elements.knowledgeable.value = cls.default_knowledgeable || 'random';
  });

  document.getElementById('btn-generate-ai-clients')?.addEventListener('click', () => {
    document.getElementById('generate-ai-clients-modal').style.display = 'flex';
  });

  document.getElementById('btn-cancel-generate-ai-clients')?.addEventListener('click', () => {
    document.getElementById('generate-ai-clients-modal').style.display = 'none';
  });

  document.getElementById('generate-ai-clients-form')?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const form = e.target;
    const count = parseInt(form.elements.count.value, 10);
    const classificationId = form.elements.classification_id.value;
    const country = form.elements.country.value;
    if (!count || count < 1 || count > 50) return;
    const btn = form.querySelector('button[type="submit"]');
    setBtnLoading(btn, true, t('generate_in_progress'));
    try {
      const res = await api.generateAIClients(count, classificationId, country);
      document.getElementById('generate-ai-clients-modal').style.display = 'none';
      form.reset();
      const filterSelect = document.getElementById('ai-client-filter');
      await loadAIClients(filterSelect ? filterSelect.value : '');
      const dupMsg = res.skipped_duplicates ? ` ${t('generate_skipped_duplicates', { count: res.skipped_duplicates })}` : '';
      alert(t('generate_success', { created: res.created_count, requested: res.requested_count }) + dupMsg);
    } catch (err) {
      alert(t('generate_failed', { error: err.message }));
    } finally {
      setBtnLoading(btn, false);
    }
  });

  document.getElementById('ai-client-filter')?.addEventListener('change', (e) => {
    selectedClassification = e.target.value;
    loadAIClients(e.target.value);
  });

  document.getElementById('ai-client-form')?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const form = e.target;
    const id = form.querySelector('[name="client_id"]').value;
    const data = collectForm(form);
    if (!data.name) return;
    const btn = form.querySelector('button[type="submit"]');
    setBtnLoading(btn, true, t('status_saving'));

    try {
      if (id) {
        await api.updateAIClient(id, data);
      } else {
        await api.createAIClient(data);
      }
      form.reset();
      document.getElementById('ai-client-form-modal').style.display = 'none';
      const filterSelect = document.getElementById('ai-client-filter');
      await loadAIClients(filterSelect ? filterSelect.value : '');
      renderAdminDashboard();
    } catch (err) {
      alert(t('ai_client_save_failed', { error: err.message }));
    } finally {
      setBtnLoading(btn, false);
    }
  });
}

function initCallsView() {
  loadCalls('');

  document.getElementById('calls-classification-filter')?.addEventListener('change', (e) => {
    loadCalls(e.target.value);
  });

  document.getElementById('calls-select-all')?.addEventListener('change', (e) => {
    if (e.target.checked) calls.forEach(c => { if (c.room) selectedCallRooms.add(c.room); });
    else selectedCallRooms.clear();
    renderCallsList();
  });

  document.getElementById('btn-delete-selected-calls')?.addEventListener('click', async () => {
    const rooms = Array.from(selectedCallRooms);
    if (!rooms.length) return;
    if (!confirm(t('results_bulk_delete_confirm', { count: rooms.length }))) return;
    const btn = document.getElementById('btn-delete-selected-calls');
    btn.disabled = true;
    btn.textContent = t('status_deleting');
    try {
      await api.bulkDeleteResults(rooms);
      selectedCallRooms.clear();
      await loadCalls(document.getElementById('calls-classification-filter')?.value || '');
    } catch (err) {
      alert(t('results_bulk_delete_failed', { error: err.message }));
      updateCallsToolbar();
    }
  });
}

let calls = [];
let selectedCallRooms = new Set();

async function loadCalls(classificationId) {
  try {
    const res = await api.getCallsList(classificationId);
    calls = res.calls || [];
  } catch (_) {
    calls = [];
  }
  renderCallsList();
}

function updateCallsToolbar() {
  const deleteBtn = document.getElementById('btn-delete-selected-calls');
  const selectAll = document.getElementById('calls-select-all');
  const selectableCount = calls.filter(c => c.room).length;
  if (deleteBtn) {
    deleteBtn.textContent = t('results_delete_selected_btn', { count: selectedCallRooms.size });
    deleteBtn.disabled = selectedCallRooms.size === 0;
  }
  if (selectAll) {
    selectAll.checked = selectableCount > 0 && selectedCallRooms.size === selectableCount;
    selectAll.indeterminate = selectedCallRooms.size > 0 && selectedCallRooms.size < selectableCount;
  }
}

function renderCallsList() {
  const container = document.getElementById('calls-list');
  if (!container) return;

  const currentRooms = new Set(calls.map(c => c.room).filter(Boolean));
  selectedCallRooms.forEach(room => { if (!currentRooms.has(room)) selectedCallRooms.delete(room); });

  if (calls.length === 0) {
    container.innerHTML = `<p class="muted">${t('calls_empty')}</p>`;
    updateCallsToolbar();
    return;
  }

  container.innerHTML = calls.map(c => `
    <div class="call-card">
      <div class="card-header">
        ${c.room ? `<label class="result-select" style="margin-inline-end:0.5rem;">
          <input type="checkbox" class="call-checkbox" data-room="${escapeHtml(c.room)}" ${selectedCallRooms.has(c.room) ? 'checked' : ''} />
        </label>` : ''}
        <h3>${escapeHtml(c.ai_client_name || t('call_default_client_name'))}</h3>
        <span class="badge ${c.status === 'completed' ? 'badge-completed' : c.status === 'started' ? 'badge-pending' : 'badge-used'}">
          ${c.status === 'completed' ? t('status_completed') : c.status === 'started' ? t('status_started') : c.status}
        </span>
      </div>
      <p class="muted">${t('call_id_label')}: ${escapeHtml(c.call_id || '')}</p>
      <p class="muted">${t('test_session_ai_client_label')}: ${escapeHtml(c.ai_client_id || '')}</p>
      <p class="muted">${t('call_classification_label')}: ${escapeHtml(c.classification_id || t('unspecified'))}</p>
      <p class="muted">${t('call_caller_label')}: ${escapeHtml(c.caller_name || '')}</p>
      <p class="muted">${t('call_started_label')}: ${escapeHtml(c.started_at || '')}</p>
      ${c.ended_at ? `<p class="muted">${t('call_ended_label')}: ${escapeHtml(c.ended_at)}</p>` : ''}
      ${c.duration ? `<p class="muted">${t('call_duration_label')}: ${t('call_duration_seconds', { seconds: c.duration })}</p>` : ''}
      ${c.score ? `<p class="muted">${t('call_score_label')}: <strong>${Math.round(parseFloat(c.score))}/100</strong></p>` : ''}
      <div class="card-actions">
        ${c.room ? `<button class="btn btn-primary download-pdf" data-room="${escapeHtml(c.room)}">${t('call_download_pdf_btn')}</button>` : ''}
        ${c.room ? `<button class="btn-danger-ghost delete-call" data-room="${escapeHtml(c.room)}">${t('result_delete_btn')}</button>` : ''}
      </div>
    </div>
  `).join('');

  container.querySelectorAll('.download-pdf').forEach(btn => {
    btn.addEventListener('click', async () => {
      setBtnLoading(btn, true, t('status_downloading'));
      try {
        const resp = await fetch(`/api/results/${encodeURIComponent(btn.dataset.room)}/pdf`, {
          headers: { 'Authorization': `Bearer ${getToken()}` }
        });
        if (!resp.ok) throw new Error('PDF not found');
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `evaluation_${btn.dataset.room}.pdf`;
        a.click();
        URL.revokeObjectURL(url);
      } catch (err) {
        alert(t('pdf_download_failed', { error: err.message }));
      } finally {
        setBtnLoading(btn, false);
      }
    });
  });

  container.querySelectorAll('.call-checkbox').forEach(cb => {
    cb.addEventListener('change', () => {
      if (cb.checked) selectedCallRooms.add(cb.dataset.room);
      else selectedCallRooms.delete(cb.dataset.room);
      updateCallsToolbar();
    });
  });

  container.querySelectorAll('.delete-call').forEach(btn => {
    btn.addEventListener('click', async () => {
      if (!confirm(t('result_delete_confirm'))) return;
      setBtnLoading(btn, true, t('status_deleting'));
      try {
        await api.deleteResult(btn.dataset.room);
        selectedCallRooms.delete(btn.dataset.room);
        await loadCalls(document.getElementById('calls-classification-filter')?.value || '');
      } catch (err) {
        alert(t('result_delete_failed', { error: err.message }));
      } finally {
        setBtnLoading(btn, false);
      }
    });
  });

  updateCallsToolbar();
}

async function loadTestSessions() {
  try {
    const res = await api.getTestSessions();
    testSessions = res.test_sessions || [];
  } catch (_) {
    testSessions = [];
  }
  renderTestSessionsList();
}

function renderTestSessionsList() {
  const container = document.getElementById('test-sessions-list');
  if (!container) return;
  
  if (testSessions.length === 0) {
    container.innerHTML = `<p class="muted">${t('test_sessions_empty')}</p>`;
    return;
  }

  container.innerHTML = testSessions.map(s => `
    <div class="test-session-card">
      <div class="card-header">
        <h3>${escapeHtml(s.candidate_name || t('unspecified'))}</h3>
        <span class="badge ${s.status === 'pending' ? 'badge-pending' : s.status === 'used' ? 'badge-used' : 'badge-completed'}">
          ${s.status === 'pending' ? t('status_pending') : s.status === 'used' ? t('status_used') : t('status_completed')}
        </span>
      </div>
      <p class="muted">${t('test_session_email_label')}: ${escapeHtml(s.candidate_email || '')}</p>
      <p class="muted">${t('test_session_id_label')}: ${escapeHtml(s.candidate_id || '')}</p>
      <p class="muted">${t('test_session_classification_label')}: ${escapeHtml(s.classification_id || t('unspecified'))}</p>
      <p class="muted">${t('test_session_ai_client_label')}: ${escapeHtml(s.ai_client_id || t('random_choice'))}</p>
      <div class="card-actions">
        ${s.status === 'pending' ? `
          <button class="btn btn-ghost" data-action="copy-link" data-id="${escapeHtml(s.attempt_id)}" data-token="${escapeHtml(s.access_token)}">${t('link_gen_copy_btn')}</button>
          <button class="btn-danger-ghost" data-action="revoke-link" data-id="${escapeHtml(s.attempt_id)}">${t('cancel')}</button>
        ` : ''}
      </div>
    </div>
  `).join('');

  container.querySelectorAll('[data-action="copy-link"]').forEach(btn => {
    btn.addEventListener('click', () => {
      const baseUrl = window.location.origin;
      const link = `${baseUrl}/?attempt=${btn.dataset.id}&token=${btn.dataset.token}`;
      copyText(link);
    });
  });

  container.querySelectorAll('[data-action="revoke-link"]').forEach(btn => {
    btn.addEventListener('click', async () => {
      if (!confirm(t('revoke_link_confirm'))) return;
      try {
        await api.revokeTestSession(btn.dataset.id);
        await loadTestSessions();
      } catch (err) {
        alert(t('revoke_link_failed', { error: err.message }));
      }
    });
  });
}

function initTestSessionsView() {
  updateLinkGenSelects();

  document.getElementById('btn-create-link')?.addEventListener('click', () => {
    const modal = document.getElementById('link-gen-modal');
    if (modal) modal.style.display = 'flex';
    const linkDiv = document.getElementById('generated-link');
    if (linkDiv) linkDiv.style.display = 'none';
  });

  document.getElementById('btn-cancel-link')?.addEventListener('click', () => {
    const modal = document.getElementById('link-gen-modal');
    if (modal) modal.style.display = 'none';
    document.getElementById('link-gen-form')?.reset();
  });

  document.getElementById('link-gen-form')?.addEventListener('submit', (e) => {
    e.preventDefault();
    submitLinkGen();
  });

  document.getElementById('btn-copy-link')?.addEventListener('click', () => {
    const linkInput = document.getElementById('generated-link-input');
    if (linkInput) copyText(linkInput.value);
  });

  loadLinkSettings();
  document.getElementById('btn-save-link-settings')?.addEventListener('click', saveLinkSettings);
}

async function loadLinkSettings() {
  const input = document.getElementById('max-reschedule-input');
  if (!input) return;
  try {
    const res = await api.getLinkSettings();
    input.value = res.max_reschedule_count;
  } catch (_) {
    input.value = 1;
  }
}

async function saveLinkSettings() {
  const input = document.getElementById('max-reschedule-input');
  const status = document.getElementById('link-settings-status');
  const btn = document.getElementById('btn-save-link-settings');
  const value = parseInt(input.value, 10);
  if (isNaN(value) || value < -1) {
    if (status) status.textContent = t('link_settings_invalid_value');
    return;
  }
  setBtnLoading(btn, true, t('status_saving'));
  try {
    await api.updateLinkSettings(value);
    if (status) {
      status.textContent = t('link_settings_saved');
      setTimeout(() => { if (status.isConnected) status.textContent = ''; }, 2000);
    }
  } catch (err) {
    if (status) status.textContent = t('link_settings_save_failed', { error: err.message });
  } finally {
    setBtnLoading(btn, false);
  }
}

async function loadResults() {
  try {
    const res = await api.getResultsList();
    results = res.results || [];
  } catch (_) {
    results = [];
  }
  renderResultsList();
}

function renderResultsList() {
  const container = document.getElementById('results-list');
  if (!container) return;

  // نشيل من التحديد أي رومات ما عادتش موجودة في القائمة الحالية
  const currentRooms = new Set(results.map(r => r._room));
  selectedResultRooms.forEach(room => { if (!currentRooms.has(room)) selectedResultRooms.delete(room); });

  if (results.length === 0) {
    container.innerHTML = `<p class="muted">${t('results_empty')}</p>`;
    updateResultsToolbar();
    return;
  }

  container.innerHTML = results.map(r => `
    <div class="result-card">
      <div class="card-header">
        <label class="result-select" style="margin-inline-end:0.5rem;">
          <input type="checkbox" class="result-checkbox" data-room="${escapeHtml(r._room)}" ${selectedResultRooms.has(r._room) ? 'checked' : ''} />
        </label>
        <h3>${escapeHtml(r.scenario_name || t('result_default_scenario_name'))}</h3>
        <span class="badge">${Math.round(r.overall_score || 0)}/100</span>
      </div>
      <p class="muted">${t('result_duration_label', { seconds: Math.round(r.duration_seconds || 0) })}</p>
      <p class="muted">${t('result_customer_label', { name: escapeHtml(r.customer_name || t('unspecified')) })}</p>
      <div class="card-actions">
        <button class="btn btn-ghost" data-action="view-result" data-room="${escapeHtml(r._room)}">${t('result_view_details_btn')}</button>
        <button class="btn btn-primary download-pdf" data-room="${escapeHtml(r._room)}">${t('result_download_pdf_btn')}</button>
        <button class="btn-danger-ghost delete-result" data-room="${escapeHtml(r._room)}">${t('result_delete_btn')}</button>
      </div>
      <div class="result-details" id="details-${escapeHtml(r._room)}" style="display:none;">
        <div class="details-content">
          <h4>${t('result_strengths_title')}</h4>
          <ul>${(r.strengths || []).map(s => `<li>${escapeHtml(s)}</li>`).join('')}</ul>
          <h4>${t('result_improvements_title')}</h4>
          <ul>${(r.areas_for_improvement || []).map(s => `<li>${escapeHtml(s)}</li>`).join('')}</ul>
          <h4>${t('result_coaching_title')}</h4>
          <p>${escapeHtml(r.coaching || t('result_coaching_none'))}</p>
          <h4>${t('result_transcript_title')}</h4>
          <div class="transcript">${(r.transcript || []).map(m => `
            <p><strong>${m.role === 'user' ? t('transcript_role_trainee') : t('transcript_role_customer')}:</strong> ${escapeHtml(m.text || '')}</p>
          `).join('')}</div>
        </div>
      </div>
    </div>
  `).join('');

  container.querySelectorAll('[data-action="view-result"]').forEach(btn => {
    btn.addEventListener('click', () => {
      const details = document.getElementById('details-' + btn.dataset.room);
      if (details) {
        details.style.display = details.style.display === 'none' ? 'block' : 'none';
        btn.textContent = details.style.display === 'none' ? t('result_view_details_btn') : t('result_hide_details_btn');
      }
    });
  });
  container.querySelectorAll('.download-pdf').forEach(btn => {
    btn.addEventListener('click', async () => {
      setBtnLoading(btn, true, t('status_downloading'));
      try {
        const resp = await fetch(`/api/results/${encodeURIComponent(btn.dataset.room)}/pdf`, {
          headers: { 'Authorization': `Bearer ${getToken()}` }
        });
        if (!resp.ok) throw new Error('PDF not found');
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `evaluation_${btn.dataset.room}.pdf`;
        a.click();
        URL.revokeObjectURL(url);
      } catch (err) {
        alert(t('pdf_download_failed', { error: err.message }));
      } finally {
        setBtnLoading(btn, false);
      }
    });
  });
  container.querySelectorAll('.result-checkbox').forEach(cb => {
    cb.addEventListener('change', () => {
      if (cb.checked) selectedResultRooms.add(cb.dataset.room);
      else selectedResultRooms.delete(cb.dataset.room);
      updateResultsToolbar();
    });
  });
  container.querySelectorAll('.delete-result').forEach(btn => {
    btn.addEventListener('click', async () => {
      if (!confirm(t('result_delete_confirm'))) return;
      setBtnLoading(btn, true, t('status_deleting'));
      try {
        await api.deleteResult(btn.dataset.room);
        selectedResultRooms.delete(btn.dataset.room);
        await loadResults();
      } catch (err) {
        alert(t('result_delete_failed', { error: err.message }));
      } finally {
        setBtnLoading(btn, false);
      }
    });
  });

  updateResultsToolbar();
}

function updateResultsToolbar() {
  const deleteBtn = document.getElementById('btn-delete-selected-results');
  const selectAll = document.getElementById('results-select-all');
  if (deleteBtn) {
    deleteBtn.textContent = t('results_delete_selected_btn', { count: selectedResultRooms.size });
    deleteBtn.disabled = selectedResultRooms.size === 0;
  }
  if (selectAll) {
    selectAll.checked = results.length > 0 && selectedResultRooms.size === results.length;
    selectAll.indeterminate = selectedResultRooms.size > 0 && selectedResultRooms.size < results.length;
  }
}

function initResultsToolbar() {
  document.getElementById('results-select-all')?.addEventListener('change', (e) => {
    if (e.target.checked) results.forEach(r => selectedResultRooms.add(r._room));
    else selectedResultRooms.clear();
    renderResultsList();
  });

  document.getElementById('btn-delete-selected-results')?.addEventListener('click', async () => {
    const rooms = Array.from(selectedResultRooms);
    if (!rooms.length) return;
    if (!confirm(t('results_bulk_delete_confirm', { count: rooms.length }))) return;
    const btn = document.getElementById('btn-delete-selected-results');
    btn.disabled = true;
    btn.textContent = t('status_deleting');
    try {
      await api.bulkDeleteResults(rooms);
      selectedResultRooms.clear();
      await loadResults(); // rebuilds the button text/counter via updateResultsToolbar()
    } catch (err) {
      alert(t('results_bulk_delete_failed', { error: err.message }));
      updateResultsToolbar();
    }
  });
}

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
  return `<span class="role-badge">${roleLabel(role)}</span>`;
}

function topbar(userName) {
  return `
    <header class="topbar">
      <div class="topbar-inner">
        <img class="logo" src="/Logo.png" alt="izam">
        <h1>Daftra Sales Heroes Arena</h1>
        <div class="topbar-user">
          <span class="muted small">${escapeHtml(userName || '')}</span>
          ${roleBadge()}
          <button class="btn-logout" id="btn-logout">${t('nav_logout')}</button>
        </div>
      </div>
    </header>`;
}

function renderLanding(error = '', formError = '', uploading = false, uploadError = '', usersMsg = '') {
  if (!user) {
    renderLogin();
    return;
  }
  
  // مستخدمين "أدمن-مثل" (عندهم أي صلاحية إدارية) بياخدوا لوحة التحكم
  if (isAdminLike(user)) {
    renderAdminDashboard();
    return;
  }

  // من هنا تحت: مستخدم عنده صلاحية "إجراء مكالمة" بس، من غير أي صلاحية إدارية —
  // القسم ده (isAdmin) بقى ميوصلوش حد عمليًا لأن أي صلاحية إدارية بترجّع لفوق،
  // بس سايبينه زي ما هو (كود قديم غير مستخدم) بدل ما نمسحه دلوقتي.
  const isAdmin = false;
  const isSdr = !!user.permissions?.make_calls;
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
      <p class="muted">عرّف بيانات العميل和他的 الشخصية. العميل الذكي هيتكلم بناءً على البيانات دي.</p>
      ${formError ? `<div class="alert">${escapeHtml(formError)}</div>` : ''}
      <form id="custom-form" class="form-grid">
        <label>اسم البروفايل (اختياري)
          <input name="name" placeholder="مثال: فهد - مؤسسة أدوية" />
        </label>
        <label>اسم العميل في المكالمة *
          <input name="customer_name" placeholder="مثال: فهد العتيبي" required />
        </label>
        <label>منصب العميل
          <input name="customer_role" placeholder="مثال: مدير المشتريات" />
        </label>
        <label>اسم الشركة
          <input name="company_name" placeholder="مثال: شركة الشفاء للأدوية" />
        </label>
        <label>مجال عمل العميل *
          <input name="business_field" placeholder="مثال: توزيع أدوية بالجملة للصيدليات" required />
        </label>
        <label>حجم الشركة
          <input name="company_size" placeholder="مثال: 50 موظفًا، 3 فروع" />
        </label>
        <label>نقطة الألم / المشكلة الأساسية
          <input name="pain_point" placeholder="مثال: إدارة المخزون بجداول إكسل يدويًا، خسائر بسبب عدم مطابقة المخزون" />
        </label>
        <label>صاحب القرار
          <select name="decision_maker">
            <option value="true">نعم، هو من يقرر</option>
            <option value="false">لا، لست صاحب القرار</option>
          </select>
        </label>
        <label>هل يعرف العميل شركة دفترة؟
          <select name="temperature">
            <option value="warm" selected>نعم، يعرفنا</option>
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
            <option value="random" selected>🎲 عشوائي</option>
            <option value="easy">سهل</option>
            <option value="medium">متوسط</option>
            <option value="hard">صعب</option>
          </select>
        </label>
        <label>شخصية العميل
          <select name="persona">
            <option value="random">🎲 عشوائي</option>
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
        <label>لغة العميل
          <select name="customer_language">
            <option value="ar" selected>🇸🇦 عربي</option>
            <option value="en">🇺🇸 English</option>
          </select>
        </label>
        <label>الصوت
          <select name="voice">
            <option value="">— تلقائي حسب اللهجة —</option>
            <option value="balRgnGuyobFvldHuixQ">Taba - سعودي</option>
            <option value="FELlZ7P5dpkH4BJsVcWt">Moazz - سعودي</option>
            <option value="SzEQh89cwBQTN77VF8m7">Salem Ahmed - سعودي</option>
            <option value="vhIzf2BLcnHOQbGSOOSe">GAWALY - سعودي</option>
            <option value="hlz7UHeM8xI2YLxXpNSY">Houzimi 2 - سعودي</option>
            <option value="EdfmSDZWwPKvZKdHoa4A">Azaam - مصري</option>
            <option value="M2ZmelgiD3eutyzRXWIZ">siso - مصري</option>
            <option value="6wEGFb9HfmAGibGevl4o">sisi - مصري</option>
            <option value="zxJ1nKbTdTEDKgscIQ5T">fla7 - مصري</option>
            <option value="vwggHPV0xUSj5rWYC6Qo">Omar Osama</option>
            <option value="DZ6l6I8upRav2eGarvdV">Hessin Abdallah</option>
            <option value="z5ZE8UcKJIRdZ8L0mw2R">meshary</option>
          </select>
        </label>
        <label>معرفة العميل بالمحاسبة
          <select name="knowledgeable">
            <option value="true" selected>محاسب/فاهم في المحاسبة</option>
            <option value="false">عميل عادي</option>
          </select>
        </label>
        <label class="full">وصف المنتج أو الخدمة (اختياري — سيتقنه العميل)
          <textarea name="product_brief" rows="4" placeholder="مثال: نظام محاسبي سحابي لإدارة المخزون والمشتريات، يدعم عدة فروع، التسعير يبدأ من 500 ريال/شهر"></textarea>
        </label>
        <button type="submit" class="btn btn-secondary full">➕ إنشاء العميل والبدء</button>
      </form>
    </section>`
    : '';

  const classificationsSection = isAdmin
    ? `
    <section class="card custom-card" id="classifications-section">
      <h2 class="section-title">📂 التصنيفات</h2>
      <p class="muted">التصنيفات تُستخدم لتنظيم العملاء الذكيين. كل تصنيف يمثل فئة عملاء (مثل: SDR، Sales، Support).</p>
      <div id="classifications-list" class="cards"></div>
      <form id="classification-form" class="form-grid" style="margin-top:1rem;">
        <input type="hidden" name="classification_id" value="" />
        <label>اسم التصنيف *
          <input name="name" placeholder="مثال: SDR Test Calls" required />
        </label>
        <label>الوصف
          <input name="description" placeholder="وصف مختصر للتصنيف" />
        </label>
        <div style="display:flex;gap:0.5rem;">
          <button type="submit" class="btn btn-secondary">💾 حفظ</button>
          <button type="button" class="btn btn-ghost" id="btn-cancel-classification">إلغاء</button>
        </div>
      </form>
    </section>`
    : '';

  const linkGenSection = isAdmin
    ? `
    <section class="card custom-card" id="link-gen-section">
      <h2 class="section-title">🔗 توليد رابط اختبار</h2>
      <p class="muted">أنشئ رابط اختبار فريد لمرشح. يعمل الرابط مرة واحدة فقط ولا يمكن إعادة استخدامه.</p>
      <form id="link-gen-form" class="form-grid">
        <label>اسم المرشح *
          <input name="candidate_name" placeholder="مثال: أحمد محمد" required />
        </label>
        <label>بريد المرشح *
          <input name="candidate_email" type="email" placeholder="مثال: ahmed@example.com" required />
        </label>
        <label>معرف المرشح (CandidateID)
          <input name="candidate_id" placeholder="اتركه فارغًا للإنشاء التلقائي" />
        </label>
        <label>التصنيف
          <select name="classification_id">
            <option value="">— اختر تصنيف —</option>
          </select>
        </label>
        <label>العميل الذكي
          <select name="ai_client_id">
            <option value="">— جميع العملاء (اختيار عشوائي) —</option>
          </select>
        </label>
        <button type="submit" class="btn btn-secondary full">🔗 توليد الرابط</button>
      </form>
      <div id="generated-link" style="margin-top:1rem;display:none;">
        <label>رابط الاختبار (انسخه وأرسله للمرشح):
          <input id="generated-link-input" readonly style="direction:ltr;font-family:monospace;" />
        </label>
        <button class="btn btn-ghost btn-inline" id="btn-copy-link" style="margin-top:0.5rem;">📋 نسخ الرابط</button>
      </div>
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
            <span class="user-role">${roleLabel(u.role)}</span>
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
      ${classificationsSection}
      ${linkGenSection}
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

  // Classification & AI Client event listeners
  if (isAdmin) {
    loadClassifications();
    loadAIClients();
    const classificationForm = document.getElementById('classification-form');
    if (classificationForm) {
      classificationForm.addEventListener('submit', (e) => {
        e.preventDefault();
        submitClassification();
      });
    }
    const btnCancelClassification = document.getElementById('btn-cancel-classification');
    if (btnCancelClassification) {
      btnCancelClassification.addEventListener('click', () => {
        classificationForm.reset();
        classificationForm.querySelector('[name="classification_id"]').value = '';
      });
    }
    const aiClientForm = document.getElementById('ai-client-form');
    if (aiClientForm) {
      aiClientForm.addEventListener('submit', (e) => {
        e.preventDefault();
        submitAIClient();
      });
    }
    const btnCancelAIClient = document.getElementById('btn-cancel-ai-client');
    if (btnCancelAIClient) {
      btnCancelAIClient.addEventListener('click', () => {
        aiClientForm.reset();
        aiClientForm.querySelector('[name="client_id"]').value = '';
      });
    }
    const filterSelect = document.getElementById('ai-client-classification-filter');
    if (filterSelect) {
      filterSelect.addEventListener('change', () => loadAIClients(filterSelect.value));
    }

    // Link generation
    const linkGenForm = document.getElementById('link-gen-form');
    if (linkGenForm) {
      linkGenForm.addEventListener('submit', (e) => {
        e.preventDefault();
        submitLinkGen();
      });
    }
    const btnCopyLink = document.getElementById('btn-copy-link');
    if (btnCopyLink) {
      btnCopyLink.addEventListener('click', () => {
        const input = document.getElementById('generated-link-input');
        if (input) copyText(input.value);
      });
    }
    // Populate classification selects for link gen
    updateLinkGenSelects();
  }
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
  if ('decision_maker' in data) data.decision_maker = data.decision_maker === 'true';
  if ('knowledgeable' in data) data.knowledgeable = data.knowledgeable === 'true';
  if ('active' in data) data.active = data.active === 'true';
  return data;
}

/* ---------------- Classifications CRUD ---------------- */

let classifications = [];

async function loadClassifications() {
  try {
    const res = await api.getClassifications();
    classifications = res.classifications || [];
  } catch (_) {
    classifications = [];
  }
  renderClassificationsList();
  renderAdminClassificationsList();
  updateClassificationSelects();
}

function renderClassificationsList() {
  const container = document.getElementById('classifications-list');
  if (!container) return;
  if (!classifications.length) {
    container.innerHTML = '<p class="muted">لا يوجد تصنيفات بعد.</p>';
    return;
  }
  container.innerHTML = classifications.map(c => `
    <div class="card scenario-card">
      <h3>${escapeHtml(c.name)}</h3>
      <p class="muted">${escapeHtml(c.description || 'بدون وصف')}</p>
      <div style="display:flex;gap:0.5rem;margin-top:0.5rem;">
        <button class="btn btn-ghost btn-inline" data-action="edit-classification" data-id="${escapeHtml(c.classification_id)}">تعديل</button>
        <button class="btn-danger-ghost" data-action="delete-classification" data-id="${escapeHtml(c.classification_id)}">حذف</button>
      </div>
    </div>
  `).join('');

  container.querySelectorAll('[data-action="edit-classification"]').forEach(btn => {
    btn.addEventListener('click', () => {
      const c = classifications.find(x => x.classification_id === btn.dataset.id);
      if (!c) return;
      const form = document.getElementById('classification-form');
      form.querySelector('[name="classification_id"]').value = c.classification_id;
      form.querySelector('[name="name"]').value = c.name;
      form.querySelector('[name="description"]').value = c.description || '';
    });
  });
  container.querySelectorAll('[data-action="delete-classification"]').forEach(btn => {
    btn.addEventListener('click', async () => {
      if (!confirm('هل أنت متأكد من حذف هذا التصنيف؟')) return;
      setBtnLoading(btn, true, 'جاري الحذف...');
      try {
        await api.deleteClassification(btn.dataset.id);
        await loadClassifications();
      } catch (err) {
        alert('Failed to delete classification: ' + err.message);
      } finally {
        setBtnLoading(btn, false);
      }
    });
  });
}

function updateClassificationSelects() {
  const selects = document.querySelectorAll('#ai-client-classification-select, #ai-client-classification-filter');
  selects.forEach(sel => {
    const current = sel.value;
    const isFilter = sel.id === 'ai-client-classification-filter';
    const defaultLabel = isFilter ? '— جميع التصنيفات —' : '— اختر تصنيف —';
    sel.innerHTML = `<option value="">${defaultLabel}</option>` +
      classifications.map(c => `<option value="${escapeHtml(c.classification_id)}">${escapeHtml(c.name)}</option>`).join('');
    sel.value = current;
  });
}

async function submitClassification() {
  const form = document.getElementById('classification-form');
  const id = form.querySelector('[name="classification_id"]').value;
  const name = form.querySelector('[name="name"]').value.trim();
  if (!name) return;
  const payload = {
    name,
    description: form.querySelector('[name="description"]').value.trim(),
    min_duration_minutes: form.querySelector('[name="min_duration_minutes"]').value.trim(),
    max_duration_minutes: form.querySelector('[name="max_duration_minutes"]').value.trim(),
    default_difficulty: form.querySelector('[name="default_difficulty"]').value,
    default_personality: form.querySelector('[name="default_personality"]').value,
    default_knowledgeable: form.querySelector('[name="default_knowledgeable"]').value,
  };

  const submitBtn = form.querySelector('button[type="submit"]');
  setBtnLoading(submitBtn, true, id ? t('status_updating') : t('status_creating'));
  showLoading(id ? t('status_updating') : t('status_creating'));
  try {
    if (id) {
      await api.updateClassification(id, payload);
    } else {
      await api.createClassification(payload);
    }
    form.reset();
    form.querySelector('[name="classification_id"]').value = '';
    const modal = document.getElementById('classification-form-modal');
    if (modal) modal.style.display = 'none';
    await loadClassifications();
  } catch (err) {
    alert(t('classification_save_failed', { error: err.message }));
  } finally {
    hideLoading();
    setBtnLoading(submitBtn, false);
  }
}

/* ---------------- AI Clients CRUD ---------------- */

let aiClients = [];

async function loadAIClients(classificationId) {
  try {
    const res = await api.getAIClients(classificationId || '');
    aiClients = res.ai_clients || [];
  } catch (_) {
    aiClients = [];
  }
  renderAIClientsList();
  renderAdminAIClientsList();
}

function renderAIClientsList() {
  const container = document.getElementById('ai-clients-list');
  if (!container) return;
  if (!aiClients.length) {
    container.innerHTML = '<p class="muted">لا يوجد عملاء ذكيين بعد.</p>';
    return;
  }
  container.innerHTML = aiClients.map(c => `
    <div class="card scenario-card">
      <div class="scenario-badge ${c.active ? '' : 'cold'}">${c.active ? 'نشط' : 'معطل'}</div>
      ${c.queue ? `<div class="scenario-badge queue-badge">${escapeHtml(c.queue)}</div>` : ''}
      <h3>${escapeHtml(c.name)}</h3>
      <p class="muted">${escapeHtml(c.customer_name || '')} · ${escapeHtml(c.customer_role || '')}</p>
      <p class="muted small">لهجة: ${escapeHtml(c.dialect)} · صعوبة: ${escapeHtml(c.difficulty)} · سيناريو: ${escapeHtml(c.scenario)}</p>
      <div style="display:flex;gap:0.5rem;margin-top:0.5rem;">
        <button class="btn btn-ghost btn-inline" data-action="edit-ai-client" data-id="${escapeHtml(c.client_id)}">تعديل</button>
        <button class="btn-danger-ghost" data-action="delete-ai-client" data-id="${escapeHtml(c.client_id)}">حذف</button>
      </div>
    </div>
  `).join('');

  container.querySelectorAll('[data-action="edit-ai-client"]').forEach(btn => {
    btn.addEventListener('click', () => {
      const c = aiClients.find(x => x.client_id === btn.dataset.id);
      if (!c) return;
      const form = document.getElementById('ai-client-form');
      form.querySelector('[name="client_id"]').value = c.client_id;
      form.querySelector('[name="name"]').value = c.name;
      form.querySelector('[name="customer_language"]').value = c.customer_language || 'ar';
      form.querySelector('[name="classification_id"]').value = c.classification_id;
      form.querySelector('[name="scenario"]').value = c.scenario;
      form.querySelector('[name="country"]').value = c.country || (c.dialect === 'saudi' ? 'sa' : 'eg');
      form.querySelector('[name="queue"]').value = c.queue || '';
      form.querySelector('[name="difficulty"]').value = c.difficulty;
      form.querySelector('[name="personality"]').value = c.personality || '';
      form.querySelector('[name="instructions"]').value = c.instructions || '';
      form.querySelector('[name="objectives"]').value = c.objectives || '';
      form.querySelector('[name="objections"]').value = c.objections || '';
      form.querySelector('[name="customer_name"]').value = c.customer_name || '';
      form.querySelector('[name="customer_role"]').value = c.customer_role || '';
      form.querySelector('[name="company_name"]').value = c.company_name || '';
      form.querySelector('[name="business_field"]').value = c.business_field || '';
      form.querySelector('[name="company_size"]').value = c.company_size || '';
      form.querySelector('[name="pain_point"]').value = c.pain_point || '';
      form.querySelector('[name="decision_maker"]').value = c.decision_maker ? 'true' : 'false';
      form.querySelector('[name="budget_sensitivity"]').value = c.budget_sensitivity || 'متوسطة';
      form.querySelector('[name="buying_intent"]').value = c.buying_intent || 'متوسطة';
      form.querySelector('[name="temperature"]').value = c.temperature || 'warm';
      form.querySelector('[name="product_brief"]').value = c.product_brief || '';
      form.querySelector('[name="voice"]').value = c.voice || '';
      form.querySelector('[name="knowledgeable"]').value = c.knowledgeable ? 'true' : 'false';
      form.querySelector('[name="active"]').value = c.active === false ? 'false' : 'true';
      form.scrollIntoView({ behavior: 'smooth' });
    });
  });
  container.querySelectorAll('[data-action="delete-ai-client"]').forEach(btn => {
    btn.addEventListener('click', async () => {
      if (!confirm('هل أنت متأكد من حذف هذا العميل الذكي؟')) return;
      setBtnLoading(btn, true, 'جاري الحذف...');
      try {
        await api.deleteAIClient(btn.dataset.id);
        const filterSelect = document.getElementById('ai-client-classification-filter');
        await loadAIClients(filterSelect ? filterSelect.value : '');
      } catch (err) {
        alert('Failed to delete AI client: ' + err.message);
      } finally {
        setBtnLoading(btn, false);
      }
    });
  });
}

function renderAdminAIClientsList() {
  const container = document.getElementById('ai-clients-grid');
  if (!container) return;
  if (!aiClients.length) {
    container.innerHTML = `<p class="muted">${t('ai_clients_empty')}</p>`;
    return;
  }
  container.innerHTML = aiClients.map(c => `
    <div class="ai-client-card" data-id="${escapeHtml(c.client_id)}">
      <div class="card-header">
        <h3>${escapeHtml(c.name)}</h3>
        <span class="badge ${c.active ? '' : 'badge-pending'}">${c.active ? t('ai_client_status_active') : t('ai_client_status_inactive')}</span>
      </div>
      ${c.queue ? `<span class="badge queue-badge">${escapeHtml(c.queue)}</span>` : ''}
      <p class="muted">${escapeHtml(c.customer_name || '')} · ${escapeHtml(c.customer_role || '')}</p>
      <p class="muted">${escapeHtml(c.company_name || '')} · ${escapeHtml(c.business_field || '')}</p>
      <div class="card-actions">
        <button class="btn btn-primary btn-start-call" data-action="start-call" data-id="${escapeHtml(c.client_id)}" data-name="${escapeHtml(c.name)}">${t('ai_client_call_btn')}</button>
        <button class="btn btn-ghost" data-action="edit-ai-client" data-id="${escapeHtml(c.client_id)}">${t('classification_edit_btn')}</button>
        <button class="btn-danger-ghost" data-action="delete-ai-client" data-id="${escapeHtml(c.client_id)}">${t('classification_delete_btn')}</button>
      </div>
    </div>
  `).join('');

  container.querySelectorAll('[data-action="edit-ai-client"]').forEach(btn => {
    btn.addEventListener('click', () => {
      const c = aiClients.find(x => x.client_id === btn.dataset.id);
      if (!c) return;
      document.getElementById('ai-client-form-modal').style.display = 'flex';
      document.getElementById('ai-client-form-title').textContent = t('ai_client_form_title_edit');
      const form = document.getElementById('ai-client-form');
      form.querySelector('[name="client_id"]').value = c.client_id;
      form.querySelector('[name="name"]').value = c.name;
      form.querySelector('[name="customer_language"]').value = c.customer_language || 'ar';
      form.querySelector('[name="classification_id"]').value = c.classification_id || '';
      form.querySelector('[name="scenario"]').value = c.scenario || 'new-lead-discovery-call';
      form.querySelector('[name="country"]').value = c.country || (c.dialect === 'saudi' ? 'sa' : 'eg');
      form.querySelector('[name="queue"]').value = c.queue || '';
      form.querySelector('[name="difficulty"]').value = c.difficulty || 'medium';
      form.querySelector('[name="personality"]').value = c.personality || '';
      form.querySelector('[name="instructions"]').value = c.instructions || '';
      form.querySelector('[name="objectives"]').value = c.objectives || '';
      form.querySelector('[name="objections"]').value = c.objections || '';
      form.querySelector('[name="customer_name"]').value = c.customer_name || '';
      form.querySelector('[name="customer_role"]').value = c.customer_role || '';
      form.querySelector('[name="company_name"]').value = c.company_name || '';
      form.querySelector('[name="business_field"]').value = c.business_field || '';
      form.querySelector('[name="company_size"]').value = c.company_size || '';
      form.querySelector('[name="pain_point"]').value = c.pain_point || '';
      form.querySelector('[name="decision_maker"]').value = c.decision_maker ? 'true' : 'false';
      form.querySelector('[name="budget_sensitivity"]').value = c.budget_sensitivity || 'متوسطة';
      form.querySelector('[name="buying_intent"]').value = c.buying_intent || 'متوسطة';
      form.querySelector('[name="temperature"]').value = c.temperature || 'warm';
      form.querySelector('[name="product_brief"]').value = c.product_brief || '';
      form.querySelector('[name="voice"]').value = c.voice || '';
      // c.knowledgeable is already the raw tristate string ('true'/'false'/'random')
      // since the backend/sheet supports "random" now — do NOT coerce with a
      // boolean ternary here, that would collapse 'random'/'false' into 'true'.
      form.querySelector('[name="knowledgeable"]').value = c.knowledgeable || 'random';
      form.querySelector('[name="active"]').value = c.active === false ? 'false' : 'true';
    });
  });

  container.querySelectorAll('[data-action="delete-ai-client"]').forEach(btn => {
    btn.addEventListener('click', async () => {
      if (!confirm(t('ai_client_delete_confirm'))) return;
      setBtnLoading(btn, true, t('status_deleting'));
      try {
        await api.deleteAIClient(btn.dataset.id);
        await loadAIClients();
        renderAdminDashboard();
      } catch (err) {
        alert(t('ai_client_delete_failed', { error: err.message }));
      } finally {
        setBtnLoading(btn, false);
      }
    });
  });

  container.querySelectorAll('[data-action="start-call"]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const clientName = btn.dataset.name || t('ai_client_default_name');
      if (!confirm(t('ai_client_start_call_confirm', { name: clientName }))) return;
      setBtnLoading(btn, true, t('status_connecting'));
      try {
        const res = await api.adminStartCall(btn.dataset.id);
        const client = aiClients.find(x => x.client_id === btn.dataset.id);
        const scenario = {
          id: res.scenario || 'new-lead-discovery-call',
          name: clientName,
          customer_name: clientName,
          customer_role: res.ai_client_name || clientName,
          country: client?.country || '',
          dialect: client?.dialect || '',
        };
        await connectCall(scenario, { url: res.url, token: res.token, room: res.room, scenario: res.scenario });
      } catch (err) {
        alert(t('ai_client_start_call_failed', { error: err.message }));
      } finally {
        setBtnLoading(btn, false);
      }
    });
  });
}

async function submitAIClient() {
  const form = document.getElementById('ai-client-form');
  const id = form.querySelector('[name="client_id"]').value;
  const data = collectForm(form);
  if (!data.name) return;

  const submitBtn = form.querySelector('button[type="submit"]');
  setBtnLoading(submitBtn, true, id ? 'جاري التحديث...' : 'جاري الإنشاء...');
  showLoading(id ? 'جاري تحديث العميل...' : 'جاري إنشاء العميل...');
  try {
    if (id) {
      await api.updateAIClient(id, data);
    } else {
      await api.createAIClient(data);
    }
    form.reset();
    form.querySelector('[name="client_id"]').value = '';
    const filterSelect = document.getElementById('ai-client-classification-filter');
    await loadAIClients(filterSelect ? filterSelect.value : '');
  } catch (err) {
    alert('Failed to save AI client: ' + err.message);
  } finally {
    hideLoading();
    setBtnLoading(submitBtn, false);
  }
}

/* ---------------- Link Generation ---------------- */

function updateLinkGenSelects() {
  const clsSelect = document.querySelector('#link-gen-form select[name="classification_id"]');
  const acSelect = document.querySelector('#link-gen-form select[name="ai_client_id"]');
  if (clsSelect) {
    clsSelect.innerHTML = `<option value="">${t('link_gen_classification_placeholder')}</option>` +
      classifications.map(c => `<option value="${escapeHtml(c.classification_id)}">${escapeHtml(c.name)}</option>`).join('');
    clsSelect.addEventListener('change', async () => {
      if (!acSelect) return;
      const clsId = clsSelect.value;
      if (!clsId) {
        acSelect.innerHTML = `<option value="">${t('link_gen_ai_client_placeholder')}</option>`;
        return;
      }
      try {
        const res = await api.getAIClients(clsId);
        const clients = res.ai_clients || [];
        acSelect.innerHTML = `<option value="">${t('link_gen_ai_client_placeholder')}</option>` +
          clients.map(c => `<option value="${escapeHtml(c.client_id)}">${escapeHtml(c.name)}</option>`).join('');
      } catch (_) {
        acSelect.innerHTML = `<option value="">${t('link_gen_ai_client_placeholder')}</option>`;
      }
    });
  }
}

async function submitLinkGen() {
  const form = document.getElementById('link-gen-form');
  const candidateName = form.querySelector('[name="candidate_name"]').value.trim();
  const candidateEmail = form.querySelector('[name="candidate_email"]').value.trim();
  const candidateId = form.querySelector('[name="candidate_id"]').value.trim();
  const classificationId = form.querySelector('[name="classification_id"]').value;
  const aiClientId = form.querySelector('[name="ai_client_id"]').value;

  if (!candidateName || !candidateEmail) return;

  const submitBtn = form.querySelector('button[type="submit"]');
  setBtnLoading(submitBtn, true, t('status_generating'));
  showLoading(t('link_gen_loading_overlay'));
  try {
    const res = await api.createAttempt({
      candidate_name: candidateName,
      candidate_email: candidateEmail,
      candidate_id: candidateId || '',
      classification_id: classificationId,
      ai_client_id: aiClientId,
    });

    const linkDiv = document.getElementById('generated-link');
    const linkInput = document.getElementById('generated-link-input');
    if (linkDiv && linkInput) {
      const baseUrl = window.location.origin;
      linkInput.value = `${baseUrl}/?attempt=${res.attempt_id}&token=${res.access_token}`;
      linkDiv.style.display = 'block';
    }
  } catch (err) {
    alert(t('link_gen_create_failed', { error: err.message }));
  } finally {
    hideLoading();
    setBtnLoading(submitBtn, false);
  }
}

async function submitCustomScenario() {
  const form = document.getElementById('custom-form');
  const brief = collectForm(form);
  const btn = form.querySelector('button[type="submit"]');
  setBtnLoading(btn, true, 'جاري الإنشاء...');
  try {
    const res = await api.createCustom(brief);
    const scenario = res.scenario;
    scenarios.push(scenario);
    renderLanding();
    startCall(scenario.id);
  } catch (err) {
    renderLanding('', `تعذّر إنشاء السيناريو: ${err.message}`);
  } finally {
    setBtnLoading(btn, false);
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
  const btn = form.querySelector('button[type="submit"]');
  setBtnLoading(btn, true, 'جاري الحفظ...');
  try {
    await api.createUser(data);
    users = (await api.getUsers()).users || [];
    renderLanding('', '', false, '', 'تم حفظ المستخدم');
  } catch (err) {
    renderLanding('', '', false, '', `تعذّر حفظ المستخدم: ${err.message}`);
  } finally {
    setBtnLoading(btn, false);
  }
}

async function deleteUser(username) {
  showLoading(`جاري حذف المستخدم ${username}...`);
  try {
    await api.deleteUser(username);
    users = (await api.getUsers()).users || [];
    renderLanding('', '', false, '', `تم حذف المستخدم ${username}`);
  } catch (err) {
    renderLanding('', '', false, '', `تعذّر حذف المستخدم: ${err.message}`);
  } finally {
    hideLoading();
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
  const btn = form.querySelector('button[type="submit"]');
  setBtnLoading(btn, true, 'جاري الإنشاء...');
  showLoading('جاري إنشاء المستخدمين...');
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
  } finally {
    hideLoading();
    setBtnLoading(btn, false);
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

function customerNationalityLabel(scenario) {
  const dialect = scenario.dialect || (scenario.country === 'eg' ? 'egyptian' : scenario.country === 'sa' ? 'saudi' : '');
  if (dialect === 'egyptian') return 'عميل مصري';
  if (dialect === 'saudi') return 'عميل سعودي';
  return 'عميل';
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
            <h2>${customerNationalityLabel(scenario)}</h2>
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
  const displayName = (user && (user.name || user.username)) || (candidate && candidate.candidate_name) || '';
  render(
    topbar(displayName) +
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
  const displayName = (user && (user.name || user.username)) || (candidate && candidate.candidate_name) || '';
  if (!result) {
    render(
      topbar(displayName) +
      `
      <main class="container center">
        <div class="card call-card">
          <h2>بطاقة التقييم غير متوفرة</h2>
          <p class="muted">تعذّر إنشاء بطاقة التقييم لهذه المكالمة. تأكد من أن العميل الآلي يعمل ثم حاول مجددًا.</p>
          <button class="btn btn-primary" id="btn-retry">🔄 إعادة السيناريو</button>
        </div>
      </main>`
    );
    document.getElementById('btn-retry').addEventListener('click', () => {
      const urlParams = parseUrlParams();
      if (urlParams.attempt) {
        renderTrialLogin();
      } else {
        renderLanding();
      }
    });
    return;
  }

  const overall = Math.max(0, Math.min(100, Math.round(Number(result.overall_score) || 0)));
  const criteria = Array.isArray(result.criteria) ? result.criteria : [];
  const configError = result.config_error || '';
  const bars = criteria
    .map((c) => {
      const value = Math.max(0, Math.min(100, Math.round(Number(c.score) || 0)));
      const weight = Number(c.weight) || 0;
      return `
        <div class="score-row">
          <span class="score-label">${escapeHtml(c.criterion || '')} <span class="muted small">(${weight}%)</span></span>
          <div class="bar"><div class="bar-fill" style="width:${value}%"></div></div>
          <span class="score-value">${value}/100</span>
        </div>`;
    })
    .join('');
  const scoresBlock = configError
    ? `<div class="warnings" style="padding:10px;"><strong>⚠️ خطأ في إعداد التقييم</strong><p>${escapeHtml(configError)}</p></div>`
    : criteria.length
      ? bars
      : '<p class="muted">لا توجد معايير تقييم متاحة.</p>';

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
    topbar(displayName) +
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
          <h3>درجات المعايير</h3>
          ${scoresBlock}
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
    const urlParams = parseUrlParams();
    if (urlParams.attempt) {
      renderTrialLogin();
    } else {
      renderLanding();
    }
  });
}

/* ---------------- Candidate Functions ---------------- */

let candidate = null;
let candidateStream = null;

function getCandidateToken() {
  return localStorage.getItem(CANDIDATE_TOKEN_KEY) || '';
}

function renderCandidateReady(error = '') {
  if (!candidate) {
    renderLogin();
    return;
  }

  render(`
    <header class="topbar">
      <div class="topbar-inner">
        <img class="logo" src="/Logo.png" alt="izam">
        <h1>Daftra AI-Simulator - Test Call</h1>
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
        <img class="logo" src="/Logo.png" alt="izam">
        <h1>Daftra AI-Simulator - Test Call</h1>
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
        <div id="self-video-wrap" style="margin: 0.5rem 0; text-align: center;">
          <video id="self-video" autoplay muted playsinline style="width: 100%; max-width: 400px; border-radius: 8px; background: #000; display: block; margin: 0 auto;"></video>
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

  // Attach candidate camera stream to the self-video element
  const selfVideo = document.getElementById('self-video');
  if (selfVideo && candidateStream) {
    selfVideo.srcObject = candidateStream;
  }

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

async function endCandidateCall() {
  stopTimer();
  // Notify server to write ended status + TestCallEndedAt immediately
  try { await api.candidateEndCall(); } catch (_) {}
  if (room) {
    try { room.disconnect(); } catch (_) {}
  }

  // Trial users see evaluation (like internal users); regular candidates see completion screen
  const urlParams = parseUrlParams();
  if (urlParams.attempt) {
    // Trial/Attempt flow — show evaluation
    renderAnalyzing('عميلك');
    const roomName = current ? current.credentials.room : null;
    if (roomName) pollResults(roomName, 0);
  } else {
    // Regular candidate — show completion, no evaluation
    renderCandidateCompletion();
  }
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
        <img class="logo" src="/Logo.png" alt="izam">
        <h1>Daftra AI-Simulator - Test Call</h1>
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
  if (candidateStream) {
    candidateStream.getTracks().forEach(track => track.stop());
    candidateStream = null;
  }

  setCandidateToken('');
  setToken('');
  candidate = null;
  user = null;
  renderLogin();
}

/* ---------------- Trial/Attempt Flow ---------------- */

let trialAttempt = null;

function getTrialToken() {
  return localStorage.getItem('trial_token') || '';
}
function setTrialToken(t) {
  if (t) localStorage.setItem('trial_token', t);
  else localStorage.removeItem('trial_token');
}

function parseUrlParams() {
  const params = new URLSearchParams(window.location.search);
  return {
    attempt: params.get('attempt') || '',
    candidate: params.get('candidate') || '',
    token: params.get('token') || '',
  };
}

function renderTrialLogin(error = '') {
  const params = parseUrlParams();
  render(`
    <header class="topbar">
      <div class="topbar-inner topbar-inner-center">
        <img class="logo" src="/Logo.png" alt="izam">
        <h1>Daftra AI-Simulator - Trial</h1>
      </div>
    </header>
    <main class="container">
      <div class="card login-card">
        <img class="login-card-logo" src="/Logo.png" alt="izam">
        <h2>Trial Test Call</h2>
        <p class="muted">Click below to start your one-time trial call.</p>
        ${error ? `<div class="alert">${escapeHtml(error)}</div>` : ''}
        <div id="trial-info"></div>
        <button id="trial-start-btn" class="btn btn-primary full" disabled>Start Trial Call</button>
        <p class="muted small" style="margin-top: 1rem;">This link can only be used once.</p>
      </div>
    </main>`);

  // Validate attempt on load
  validateTrialAttempt(params.attempt, params.token);
}

async function validateTrialAttempt(attemptId, accessToken) {
  const infoDiv = document.getElementById('trial-info');
  const startBtn = document.getElementById('trial-start-btn');

  if (!attemptId || !accessToken) {
    if (infoDiv) infoDiv.innerHTML = '<p style="color:red;">Invalid trial link.</p>';
    return;
  }

  try {
    const res = await api.get(`/api/attempts/${encodeURIComponent(attemptId)}?token=${encodeURIComponent(accessToken)}`);
    trialAttempt = res;
    if (infoDiv) {
      infoDiv.innerHTML = `
        <p><strong>Candidate:</strong> ${escapeHtml(res.candidate_name)}</p>
        <p><strong>Email:</strong> ${escapeHtml(res.candidate_email)}</p>
      `;
    }
    if (startBtn) startBtn.disabled = false;
  } catch (err) {
    if (infoDiv) {
      const msg = err.message.includes('410') ? 'This trial link has already been used.' :
                  err.message.includes('404') ? 'Invalid or expired trial link.' :
                  'Failed to validate trial link.';
      infoDiv.innerHTML = `<p style="color:red;">${msg}</p>`;
    }
  }
}

async function startTrialCall() {
  if (!trialAttempt) return;
  const startBtn = document.getElementById('trial-start-btn');
  if (startBtn) {
    startBtn.disabled = true;
    startBtn.textContent = 'Starting...';
  }

  try {
    // Authenticate as candidate for this attempt
    const loginRes = await api.post('/api/candidate/login', {
      email: trialAttempt.candidate_email,
      candidate_id: trialAttempt.candidate_id,
    });

    if (loginRes.status === 'ended' || loginRes.status === 'started') {
      renderTrialLogin(loginRes.message || 'Call already completed.');
      return;
    }

    setCandidateToken(loginRes.token);
    candidate = loginRes.candidate;

    // Start call with attempt context
    const callRes = await api.post('/api/candidate/start-call', {
      attempt_id: trialAttempt.attempt_id,
    });

    // Consume the attempt
    try {
      await api.consumeAttempt(trialAttempt.attempt_id);
    } catch (_) {}

    renderCandidateCall(callRes);
  } catch (err) {
    if (startBtn) {
      startBtn.disabled = false;
      startBtn.textContent = 'Start Trial Call';
    }
    renderTrialLogin('Failed to start trial: ' + err.message);
  }
}

/* ---------------- Auto-generated Scheduled Test Call Link ----------------
   Distinct from the "Trial/Attempt" flow above: this link is generated
   automatically ahead of a real candidate's own TestCallScheduledAt (from the
   Candidates sheet), identified by candidate_id + a secure token stored on
   their own row — not an Attempts-tab AttemptID. */

let scheduledLinkCandidate = null;

function renderScheduledCallLogin(error = '') {
  const params = parseUrlParams();
  render(`
    <header class="topbar">
      <div class="topbar-inner topbar-inner-center">
        <img class="logo" src="/Logo.png" alt="izam">
        <h1>Daftra AI-Simulator - Test Call</h1>
      </div>
    </header>
    <main class="container">
      <div class="card login-card">
        <img class="login-card-logo" src="/Logo.png" alt="izam">
        <h2>Scheduled Test Call</h2>
        <p class="muted">Click below to start your test call.</p>
        ${error ? `<div class="alert">${escapeHtml(error)}</div>` : ''}
        <div id="scheduled-call-info"></div>
        <button id="scheduled-call-start-btn" class="btn btn-primary full" disabled>Start Test Call</button>
        <p class="muted small" style="margin-top: 1rem;">This link can only be used once.</p>
      </div>
    </main>`);

  validateScheduledCallLink(params.candidate, params.token);
}

async function validateScheduledCallLink(candidateId, token) {
  const infoDiv = document.getElementById('scheduled-call-info');
  const startBtn = document.getElementById('scheduled-call-start-btn');

  if (!candidateId || !token) {
    if (infoDiv) infoDiv.innerHTML = '<p style="color:red;">Invalid test call link.</p>';
    return;
  }

  try {
    const res = await api.get(`/api/candidate-link?candidate_id=${encodeURIComponent(candidateId)}&token=${encodeURIComponent(token)}`);
    scheduledLinkCandidate = { candidate_id: candidateId, token, ...res };
    if (infoDiv) {
      infoDiv.innerHTML = `
        <p><strong>Candidate:</strong> ${escapeHtml(res.candidate_name)}</p>
        <p><strong>Email:</strong> ${escapeHtml(res.candidate_email)}</p>
      `;
    }
    if (startBtn) startBtn.disabled = false;
  } catch (err) {
    if (infoDiv) {
      const msg = err.message.includes('410') && err.message.toLowerCase().includes('expired')
        ? 'This test call link has expired.'
        : err.message.includes('410') ? 'This test call link has already been used.'
        : err.message.includes('404') ? 'Invalid test call link.'
        : 'Failed to validate test call link.';
      infoDiv.innerHTML = `<p style="color:red;">${msg}</p>`;
    }
  }
}

async function startScheduledCall() {
  if (!scheduledLinkCandidate) return;
  const startBtn = document.getElementById('scheduled-call-start-btn');
  if (startBtn) {
    startBtn.disabled = true;
    startBtn.textContent = 'Starting...';
  }

  try {
    const loginRes = await api.post('/api/candidate-link/login', {
      candidate_id: scheduledLinkCandidate.candidate_id,
      token: scheduledLinkCandidate.token,
    });

    setCandidateToken(loginRes.token);
    candidate = loginRes.candidate;

    const callRes = await api.post('/api/candidate/start-call', {});
    renderCandidateCall(callRes);
  } catch (err) {
    if (startBtn) {
      startBtn.disabled = false;
      startBtn.textContent = 'Start Test Call';
    }
    renderScheduledCallLogin('Failed to start call: ' + err.message);
  }
}

/* ---------------- Boot ---------------- */

async function boot() {
  applyLanguageAttrs();
  // Check for trial/attempt link first
  const urlParams = parseUrlParams();
  if (urlParams.attempt && urlParams.token) {
    renderTrialLogin();
    document.getElementById('trial-start-btn')?.addEventListener('click', startTrialCall);
    return;
  }

  // Check for an auto-generated scheduled test-call link (candidate_id + token
  // on the candidate's own Candidates row, not an Attempts-tab AttemptID)
  if (urlParams.candidate && urlParams.token) {
    renderScheduledCallLogin();
    document.getElementById('scheduled-call-start-btn')?.addEventListener('click', startScheduledCall);
    return;
  }

  // Check for candidate token
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
  const internalToken = localStorage.getItem(TOKEN_KEY);
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

boot().catch(err => {
  console.error('Boot error:', err);
  document.getElementById('app').innerHTML = '<div style="padding:40px;font-family:sans-serif;"><h2>Application Error</h2><pre>' + err.message + '</pre></div>';
});