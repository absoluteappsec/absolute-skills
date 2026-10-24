/* Fixture SPA: Vue 3 + vue-router 4 (history mode). */
const { createApp, reactive, ref, onMounted, onBeforeUnmount } = Vue;
const { createRouter, createWebHistory } = VueRouter;

// Referenced for bundle-mining tests; deliberately never fetched.
const INTERNAL_FLAGS_ENDPOINT = "/api/internal/flags";

const auth = reactive({
  token: localStorage.getItem("token"),
  role: localStorage.getItem("role"),
  username: localStorage.getItem("username"),
});

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (auth.token) headers["Authorization"] = "Bearer " + auth.token;
  const res = await fetch(path, { ...opts, headers });
  return { status: res.status, data: await res.json().catch(() => null) };
}

const Home = {
  template: `
    <section>
      <h2>Home</h2>
      <form v-if="!auth.token" @submit.prevent="login">
        <input name="username" v-model="username" placeholder="username">
        <input name="password" type="password" v-model="password" placeholder="password">
        <button type="submit">Sign in</button>
        <p class="error" v-if="error">{{ error }}</p>
      </form>
      <div v-else>
        <p>Signed in as {{ auth.username }} ({{ auth.role }})</p>
        <button @click="logout">Sign out</button>
        <p>Featured: {{ featured.join(', ') }}</p>
      </div>
    </section>`,
  setup() {
    const username = ref(""), password = ref(""), error = ref(""), featured = ref([]);
    let es = null;
    async function loadFeatured() {
      if (!auth.token) return;
      const r = await api("/graphql", { method: "POST",
        body: JSON.stringify({ query: "query ListItems { items { id title } }" }) });
      featured.value = r.data && r.data.data ? r.data.data.items.slice(0, 3).map(i => i.title) : [];
    }
    async function login() {
      const r = await api("/api/login", { method: "POST",
        body: JSON.stringify({ username: username.value, password: password.value }) });
      if (r.status !== 200) { error.value = "Login failed"; return; }
      auth.token = r.data.token; auth.role = r.data.role; auth.username = r.data.username;
      localStorage.setItem("token", auth.token);
      localStorage.setItem("role", auth.role);
      localStorage.setItem("username", auth.username);
      loadFeatured();
    }
    function logout() {
      auth.token = auth.role = auth.username = null;
      ["token", "role", "username"].forEach(k => localStorage.removeItem(k));
    }
    onMounted(() => {
      loadFeatured();
      es = new EventSource("/api/events");
      es.onmessage = () => {};
      es.onerror = () => es.close();
    });
    onBeforeUnmount(() => es && es.close());
    return { auth, username, password, error, featured, login, logout };
  },
};

const Items = {
  template: `
    <section>
      <h2>Items</h2>
      <div class="new-item">
        <input class="title" v-model="title" placeholder="Title">
        <div class="combo" role="combobox" :aria-expanded="open" tabindex="0" @click="open = !open">
          {{ category || 'Select category' }}
          <ul v-if="open" role="listbox">
            <li v-for="c in categories" role="option" :key="c" @click.stop="pick(c)">{{ c }}</li>
          </ul>
        </div>
        <button @click="save">Save</button>
        <span class="status">{{ status }}</span>
      </div>
      <div class="card" v-for="item in items" :key="item.id" @click="open_item(item)">
        {{ item.title }}
      </div>
      <div ref="sentinel" id="sentinel" style="height: 20px"></div>
      <p v-if="!cursorMore">End of list</p>
    </section>`,
  setup() {
    const router = VueRouter.useRouter();
    const items = ref([]), title = ref(""), category = ref(""), open = ref(false), status = ref("");
    const sentinel = ref(null), cursorMore = ref(true);
    const categories = ["general", "urgent", "archive"];
    let cursor = "", loading = false, observer = null;
    async function more() {
      if (loading || !cursorMore.value) return;
      loading = true;
      const r = await api("/api/items?cursor=" + encodeURIComponent(cursor));
      if (r.status === 200) {
        items.value.push(...r.data.items);
        cursor = r.data.next_cursor;
        cursorMore.value = !!cursor;
      } else cursorMore.value = false;
      loading = false;
    }
    function open_item(item) { router.push("/items/" + item.id); }
    function pick(c) { category.value = c; open.value = false; }
    async function save() {
      const r = await api("/api/items", { method: "POST",
        body: JSON.stringify({ title: title.value, category: category.value }) });
      status.value = r.status === 201 ? "Saved #" + r.data.id : "Error";
      if (r.status === 201) items.value.unshift(r.data);
    }
    onMounted(() => {
      observer = new IntersectionObserver(es => es.some(e => e.isIntersecting) && more());
      observer.observe(sentinel.value);
      more();
    });
    onBeforeUnmount(() => observer && observer.disconnect());
    return { items, title, category, open, status, sentinel, cursorMore, categories,
             open_item, pick, save };
  },
};

const ItemDetail = {
  template: `
    <section>
      <h2>Item {{ $route.params.id }}</h2>
      <p v-if="item">{{ item.title }} (owner: {{ item.owner }})</p>
      <p v-else>{{ message }}</p>
      <button @click="remove">Delete</button>
    </section>`,
  setup() {
    const route = VueRouter.useRoute(), router = VueRouter.useRouter();
    const item = ref(null), message = ref("Loading...");
    onMounted(async () => {
      const r = await api("/api/items/" + route.params.id);
      if (r.status === 200) item.value = r.data; else message.value = "Not available";
    });
    async function remove() {
      await api("/graphql", { method: "POST", body: JSON.stringify({
        query: "mutation DeleteItem($id: Int!) { deleteItem(id: $id) }",
        variables: { id: Number(route.params.id) } }) });
      router.push("/items");
    }
    return { item, message, remove };
  },
};

const Settings = { template: `<section><h2>Settings</h2><p>Preferences (unlinked route).</p></section>` };

const Admin = {
  template: `<section><h2>Admin</h2><pre>{{ stats }}</pre></section>`,
  setup() {
    const stats = ref("");
    onMounted(async () => { stats.value = JSON.stringify((await api("/api/admin/stats")).data); });
    return { stats };
  },
};

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", component: Home },
    { path: "/items", component: Items },
    { path: "/items/:id", component: ItemDetail },
    { path: "/settings", component: Settings },
    { path: "/admin", component: Admin },
  ],
});
router.beforeEach(to => {
  if (to.path === "/admin" && auth.role !== "admin") return "/";
});

createApp({
  template: `
    <header>
      <nav>
        <router-link to="/">Home</router-link>
        <router-link to="/items">Items</router-link>
        <router-link v-if="auth.role === 'admin'" to="/admin">Admin</router-link>
      </nav>
    </header>
    <main><router-view /></main>`,
  setup() { return { auth }; },
}).use(router).mount("#app");
