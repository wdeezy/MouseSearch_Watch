/**
 * Watches UI: saved searches that run on a schedule and auto-grab new releases.
 *
 * Talks to the JSON API under /api/watches (see watches/routes.py). Relies on a
 * few globals from main.js: APP_BASE, showToast, loadLegacyCategoryDefinitions,
 * and (optionally) TomSelect for the multi-selects.
 */
(function () {
    'use strict';

    const BASE = (typeof APP_BASE === 'string' ? APP_BASE : (window.APP_BASE || '')).replace(/\/+$/, '');
    const API = `${BASE}/api/watches`;

    const state = {
        watches: [],
        editing: null,      // watch being edited (or null for new)
        lastResult: null,   // last test/run summary
        lastResultWatchId: null,
        eventsWatchId: null,
        selectsReady: null, // promise
        tomSelects: {},
        status: { enabled: null },
    };

    const $ = (id) => document.getElementById(id);
    const toast = (message, type) => {
        if (typeof showToast === 'function') showToast(message, type);
        else console.log(`[watches] ${type}: ${message}`);
    };

    function escapeHtml(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function escapeRegex(value) {
        return String(value ?? '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    }

    function relativeTime(iso) {
        if (!iso) return 'never';
        const then = new Date(iso);
        if (Number.isNaN(then.getTime())) return iso;
        const diff = Math.round((Date.now() - then.getTime()) / 1000);
        if (diff < 60) return 'just now';
        if (diff < 3600) return `${Math.round(diff / 60)} min ago`;
        if (diff < 86400) return `${Math.round(diff / 3600)} h ago`;
        return `${Math.round(diff / 86400)} d ago`;
    }

    function formatTimestamp(iso) {
        if (!iso) return '';
        const date = new Date(iso);
        return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
    }

    async function api(path, options = {}) {
        const init = Object.assign({ headers: { 'Content-Type': 'application/json' } }, options);
        if (init.body && typeof init.body !== 'string') init.body = JSON.stringify(init.body);
        const response = await fetch(`${API}${path}`, init);
        let data = {};
        try { data = await response.json(); } catch (_) { /* no body */ }
        if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
        return data;
    }

    // ------------------------------------------------------------------ views
    const VIEWS = ['watches-list-view', 'watch-form-view', 'watch-result-view', 'watch-events-view'];

    function showView(id) {
        VIEWS.forEach(viewId => $(viewId)?.classList.toggle('d-none', viewId !== id));
        $('watchesOffcanvas')?.querySelector('.offcanvas-body')?.scrollTo({ top: 0 });
    }

    // ------------------------------------------------------------------ status + list
    async function loadStatus() {
        try {
            const status = await api('/status');
            state.status = status;
            $('watches-disabled-alert')?.classList.toggle('d-none', !!status.enabled);
            const badge = $('watches-count-badge');
            if (badge) {
                badge.textContent = status.watch_count || 0;
                badge.style.display = status.watch_count ? '' : 'none';
            }
        } catch (_) { /* ignore */ }
    }

    async function loadWatches() {
        try {
            const data = await api('');
            state.watches = data.watches || [];
            renderList();
            const badge = $('watches-count-badge');
            if (badge) {
                badge.textContent = state.watches.length;
                badge.style.display = state.watches.length ? '' : 'none';
            }
        } catch (error) {
            toast(error.message || 'Unable to load watches.', 'danger');
        }
    }

    function describeSearch(watch) {
        const bits = [];
        if (watch.query) bits.push(`“${watch.query}”`);
        if (watch.title_regex) bits.push(`<code>${escapeHtml(watch.title_regex)}</code>`);
        return bits.join(' &middot; ') || '<em>empty search</em>';
    }

    function renderList() {
        const list = $('watches-list');
        const empty = $('watches-empty');
        const summary = $('watches-summary');
        if (!list) return;

        list.innerHTML = '';
        const total = state.watches.length;
        const enabled = state.watches.filter(w => w.enabled).length;
        if (summary) summary.textContent = total ? `${total} watch${total === 1 ? '' : 'es'}, ${enabled} enabled` : '';
        empty?.classList.toggle('d-none', total > 0);

        state.watches.forEach(watch => {
            const card = document.createElement('div');
            card.className = `card watch-card border-secondary-subtle ${watch.enabled ? '' : 'opacity-75'}`;
            card.dataset.watchId = watch.id;
            const errorHtml = watch.last_error
                ? `<div class="text-danger small text-truncate" title="${escapeHtml(watch.last_error)}"><i class="bi bi-exclamation-circle me-1"></i>${escapeHtml(watch.last_error)}</div>`
                : '';
            card.innerHTML = `
              <div class="card-body py-2 px-3">
                <div class="d-flex justify-content-between align-items-start gap-2">
                  <div class="flex-grow-1 min-w-0">
                    <div class="fw-semibold text-truncate">${escapeHtml(watch.name)}</div>
                    <div class="small text-body-secondary text-truncate">${describeSearch(watch)}</div>
                  </div>
                  <div class="form-check form-switch m-0" title="${watch.enabled ? 'Enabled' : 'Disabled'}">
                    <input class="form-check-input watch-enabled-toggle" type="checkbox" ${watch.enabled ? 'checked' : ''} data-watch-id="${watch.id}">
                  </div>
                </div>
                <div class="d-flex flex-wrap gap-3 small text-body-secondary mt-1">
                  <span title="Interval"><i class="bi bi-clock me-1"></i>every ${escapeHtml(watch.interval_minutes)} min</span>
                  <span title="${escapeHtml(formatTimestamp(watch.last_run_at))}"><i class="bi bi-arrow-repeat me-1"></i>last run ${escapeHtml(relativeTime(watch.last_run_at))}</span>
                  ${watch.last_summary ? `<span title="Last result"><i class="bi bi-clipboard-data me-1"></i>${escapeHtml(watch.last_summary)}</span>` : ''}
                  ${watch.torrent_category ? `<span title="Client category"><i class="bi bi-tag me-1"></i>${escapeHtml(watch.torrent_category)}</span>` : ''}
                </div>
                ${errorHtml}
                <div class="d-flex flex-wrap gap-1 mt-2">
                  <button class="btn btn-sm btn-outline-primary" type="button" data-watch-action="test" data-watch-id="${watch.id}" title="Search and filter without grabbing"><i class="bi bi-flask me-1"></i>Test</button>
                  <button class="btn btn-sm btn-outline-success" type="button" data-watch-action="run" data-watch-id="${watch.id}" title="Run now and grab new matches"><i class="bi bi-play-fill me-1"></i>Run now</button>
                  <button class="btn btn-sm btn-outline-secondary" type="button" data-watch-action="events" data-watch-id="${watch.id}"><i class="bi bi-clock-history me-1"></i>History</button>
                  <button class="btn btn-sm btn-outline-secondary" type="button" data-watch-action="edit" data-watch-id="${watch.id}"><i class="bi bi-pencil me-1"></i>Edit</button>
                  <button class="btn btn-sm btn-outline-danger ms-auto" type="button" data-watch-action="delete" data-watch-id="${watch.id}"><i class="bi bi-trash"></i></button>
                </div>
              </div>`;
            list.appendChild(card);
        });
    }

    // ------------------------------------------------------------------ selects
    function getSelectValues(id) {
        const ts = state.tomSelects[id];
        if (ts) return [].concat(ts.getValue() || []).map(String).filter(Boolean);
        const select = $(id);
        return select ? Array.from(select.selectedOptions).map(o => o.value).filter(Boolean) : [];
    }

    function setSelectValues(id, values) {
        const wanted = (values || []).map(String);
        const ts = state.tomSelects[id];
        if (ts) {
            ts.setValue(wanted, true);
            return;
        }
        const select = $(id);
        if (!select) return;
        Array.from(select.options).forEach(option => { option.selected = wanted.includes(option.value); });
    }

    function initTomSelect(id, extra = {}) {
        const element = $(id);
        if (!element || typeof TomSelect === 'undefined' || element.tomselect) return;
        try {
            state.tomSelects[id] = new TomSelect(element, Object.assign({
                plugins: ['remove_button', 'checkbox_options', 'clear_button'],
                create: false,
                maxItems: null,
                maxOptions: 1000,
                hidePlaceholder: true,
            }, extra));
        } catch (error) {
            console.warn('[watches] TomSelect init failed for', id, error);
        }
    }

    async function loadCategoryDefinitions() {
        if (typeof loadLegacyCategoryDefinitions === 'function') {
            return loadLegacyCategoryDefinitions();
        }
        try {
            const response = await fetch(`${BASE}/static/categoryDefinitionsLegacy.json`, { cache: 'no-store' });
            return response.ok ? response.json() : null;
        } catch (_) {
            return null;
        }
    }

    async function loadClientCategories() {
        const select = $('watch-torrent-category');
        if (!select) return;
        try {
            const response = await fetch(`${BASE}/client/categories`);
            if (!response.ok) return;
            const data = await response.json();
            const names = Array.isArray(data) ? data : Object.keys(data || {});
            const current = select.value;
            select.innerHTML = '<option value="">Default category</option>';
            names.sort((a, b) => String(a).localeCompare(String(b))).forEach(name => {
                const option = document.createElement('option');
                option.value = name;
                option.textContent = name;
                select.appendChild(option);
            });
            select.value = current;
        } catch (_) { /* leave default option */ }
    }

    function ensureSelectsReady() {
        if (state.selectsReady) return state.selectsReady;
        state.selectsReady = (async () => {
            const catSelect = $('watch-category-ids');
            const definitions = await loadCategoryDefinitions();
            if (catSelect && definitions?.categories?.length) {
                catSelect.innerHTML = '';
                definitions.categories.forEach(mainCat => {
                    const group = document.createElement('optgroup');
                    group.label = mainCat.name;
                    (mainCat.subcategories || []).forEach(subcat => {
                        const option = document.createElement('option');
                        option.value = String(subcat.category);
                        option.textContent = subcat.name;
                        group.appendChild(option);
                    });
                    catSelect.appendChild(group);
                });
            }
            initTomSelect('watch-language-ids');
            initTomSelect('watch-main-cat');
            initTomSelect('watch-category-ids');
            await loadClientCategories();
        })();
        return state.selectsReady;
    }

    // ------------------------------------------------------------------ form
    function ensureCategoryOption(value) {
        const select = $('watch-torrent-category');
        if (!select || !value) return;
        if (!Array.from(select.options).some(o => o.value === value)) {
            const option = document.createElement('option');
            option.value = value;
            option.textContent = value;
            select.appendChild(option);
        }
    }

    async function openForm(watch, prefill) {
        await ensureSelectsReady();
        const source = Object.assign({
            id: '', name: '', enabled: true, query: '', title_regex: '',
            search_fields: ['title'], language_ids: [], main_cat: [], category_ids: [],
            flag_ids: [], flags_mode: '0', search_type: 'all', min_seeders: '',
            interval_minutes: 60, max_grabs_per_run: 3, torrent_category: '',
        }, watch || {}, prefill || {});

        state.editing = watch || null;
        $('watch-form-title').textContent = watch ? `Edit watch: ${watch.name}` : 'New watch';
        $('watch-id').value = watch?.id || '';
        $('watch-name').value = source.name || '';
        $('watch-enabled').checked = !!source.enabled;
        $('watch-query').value = source.query || '';
        $('watch-title-regex').value = source.title_regex || '';
        const fields = new Set((source.search_fields || ['title']).map(String));
        document.querySelectorAll('.watch-search-field').forEach(cb => { cb.checked = fields.has(cb.value); });
        setSelectValues('watch-language-ids', source.language_ids || []);
        setSelectValues('watch-main-cat', (source.main_cat || []).filter(v => v !== 'all'));
        setSelectValues('watch-category-ids', source.category_ids || []);
        const flags = new Set((source.flag_ids || []).map(String));
        document.querySelectorAll('.watch-flag').forEach(cb => { cb.checked = flags.has(cb.value); });
        $('watch-flags-mode').value = String(source.flags_mode || '0');
        $('watch-search-type').value = source.search_type || 'all';
        $('watch-min-seeders').value = source.min_seeders ?? '';
        $('watch-interval').value = source.interval_minutes || 60;
        $('watch-max-grabs').value = source.max_grabs_per_run || 3;
        ensureCategoryOption(source.torrent_category);
        $('watch-torrent-category').value = source.torrent_category || '';
        $('watch-delete').classList.toggle('d-none', !watch);
        hideFormError();
        showView('watch-form-view');
    }

    function readForm() {
        const minSeeders = $('watch-min-seeders').value.trim();
        return {
            id: $('watch-id').value ? Number($('watch-id').value) : undefined,
            name: $('watch-name').value.trim(),
            enabled: $('watch-enabled').checked,
            query: $('watch-query').value.trim(),
            title_regex: $('watch-title-regex').value.trim(),
            search_fields: Array.from(document.querySelectorAll('.watch-search-field:checked')).map(cb => cb.value),
            language_ids: getSelectValues('watch-language-ids'),
            main_cat: getSelectValues('watch-main-cat'),
            category_ids: getSelectValues('watch-category-ids'),
            flag_ids: Array.from(document.querySelectorAll('.watch-flag:checked')).map(cb => cb.value),
            flags_mode: $('watch-flags-mode').value,
            search_type: $('watch-search-type').value,
            min_seeders: minSeeders === '' ? null : Number(minSeeders),
            interval_minutes: Number($('watch-interval').value || 60),
            max_grabs_per_run: Number($('watch-max-grabs').value || 3),
            torrent_category: $('watch-torrent-category').value,
        };
    }

    function showFormError(message) {
        const box = $('watch-form-error');
        if (!box) return;
        box.textContent = message;
        box.classList.remove('d-none');
    }

    function hideFormError() {
        $('watch-form-error')?.classList.add('d-none');
    }

    async function saveWatch() {
        const data = readForm();
        hideFormError();
        try {
            const result = data.id
                ? await api(`/${data.id}`, { method: 'PUT', body: data })
                : await api('', { method: 'POST', body: data });
            toast(result.message || 'Watch saved.', 'success');
            await loadWatches();
            showView('watches-list-view');
        } catch (error) {
            showFormError(error.message || 'Unable to save watch.');
        }
    }

    async function deleteWatch(id) {
        const watch = state.watches.find(w => String(w.id) === String(id));
        if (!window.confirm(`Delete watch "${watch?.name || id}"? Its history and seen-list go with it.`)) return;
        try {
            const result = await api(`/${id}`, { method: 'DELETE' });
            toast(result.message || 'Watch deleted.', 'success');
            await loadWatches();
            showView('watches-list-view');
        } catch (error) {
            toast(error.message || 'Unable to delete watch.', 'danger');
        }
    }

    async function toggleEnabled(id, enabled) {
        try {
            await api(`/${id}`, { method: 'PUT', body: { enabled } });
            await loadWatches();
        } catch (error) {
            toast(error.message || 'Unable to update watch.', 'danger');
            await loadWatches();
        }
    }

    // ------------------------------------------------------------------ test / run
    function setBusy(button, busy, label) {
        if (!button) return;
        if (busy) {
            button.dataset.originalHtml = button.innerHTML;
            button.disabled = true;
            button.innerHTML = `<span class="spinner-border spinner-border-sm me-1" role="status" aria-hidden="true"></span>${label || 'Working…'}`;
        } else {
            button.disabled = false;
            if (button.dataset.originalHtml) button.innerHTML = button.dataset.originalHtml;
        }
    }

    const ACTION_LABELS = {
        no_match: ['—', 'text-body-secondary', 'Title did not match the regex'],
        skipped_seen: ['No', 'text-body-secondary', 'Already seen'],
        skipped_cap: ['Later', 'text-warning', 'Over the per-run cap; retried next run'],
        would_grab: ['Yes', 'text-success fw-semibold', 'Would be sent to the client'],
        grabbed: ['Grabbed', 'text-success fw-semibold', 'Sent to the client'],
        error: ['Error', 'text-danger', ''],
    };

    function renderResult(summary, { watchId, title } = {}) {
        state.lastResult = summary;
        state.lastResultWatchId = watchId ?? null;
        $('watch-result-title').textContent = title || (summary.dry_run ? 'Test result' : 'Run result');
        $('watch-result-summary').textContent = summary.summary_text || '';

        const errorBox = $('watch-result-error');
        if (summary.error) {
            errorBox.textContent = summary.error;
            errorBox.classList.remove('d-none');
        } else {
            errorBox.classList.add('d-none');
        }

        const body = $('watch-result-body');
        body.innerHTML = '';
        const items = summary.items || [];
        if (!items.length) {
            body.innerHTML = '<tr><td colspan="5" class="text-center text-body-secondary py-3">The search returned no results.</td></tr>';
        }
        items.forEach(item => {
            const [label, cls, hint] = ACTION_LABELS[item.action] || [item.action, '', ''];
            const row = document.createElement('tr');
            row.className = item.matched ? '' : 'opacity-50';
            row.innerHTML = `
              <td class="text-break">${escapeHtml(item.title)}<div class="text-body-secondary" style="font-size:.75rem;">#${escapeHtml(item.id)}${item.size ? ` &middot; ${escapeHtml(item.size)}` : ''}${item.seeders != null ? ` &middot; ${escapeHtml(item.seeders)} seeders` : ''}</div></td>
              <td class="text-nowrap">${escapeHtml(String(item.added || '').slice(0, 10))}</td>
              <td class="text-center">${item.matched ? '<i class="bi bi-check-lg text-success"></i>' : '<i class="bi bi-dash text-body-secondary"></i>'}</td>
              <td class="text-center">${item.seen ? '<i class="bi bi-eye-fill text-body-secondary"></i>' : '<i class="bi bi-dash text-body-secondary"></i>'}</td>
              <td class="text-center ${cls}" title="${escapeHtml(item.detail || hint)}">${escapeHtml(label)}</td>`;
            body.appendChild(row);
        });

        const canMarkSeen = !!watchId && items.some(item => item.matched && !item.seen);
        $('watch-result-mark-seen').classList.toggle('d-none', !canMarkSeen);
        $('watch-result-run').classList.toggle('d-none', !watchId);
        $('watch-result-retest').classList.toggle('d-none', !watchId && !state.editing && !$('watch-form-view'));
        showView('watch-result-view');
    }

    async function testWatch(id, button) {
        setBusy(button, true, 'Testing…');
        try {
            const summary = await api(`/${id}/test`, { method: 'POST' });
            const watch = state.watches.find(w => String(w.id) === String(id));
            renderResult(summary, { watchId: id, title: `Test: ${watch?.name || summary.name || id}` });
        } catch (error) {
            toast(error.message || 'Test failed.', 'danger');
        } finally {
            setBusy(button, false);
        }
    }

    async function testForm(button) {
        const data = readForm();
        hideFormError();
        setBusy(button, true, 'Testing…');
        try {
            const summary = data.id
                ? await api(`/${data.id}/test`, { method: 'POST' })
                : await api('/test', { method: 'POST', body: data });
            if (data.id) {
                renderResult(summary, { watchId: data.id, title: `Test: ${data.name}` });
            } else {
                renderResult(summary, { title: `Test (unsaved): ${data.name || 'new watch'}` });
            }
        } catch (error) {
            showFormError(error.message || 'Test failed.');
        } finally {
            setBusy(button, false);
        }
    }

    async function runWatch(id, button) {
        const watch = state.watches.find(w => String(w.id) === String(id));
        if (!window.confirm(`Run "${watch?.name || id}" now? New matches will be sent to the torrent client.`)) return;
        setBusy(button, true, 'Running…');
        try {
            const summary = await api(`/${id}/run`, { method: 'POST' });
            renderResult(summary, { watchId: id, title: `Run: ${watch?.name || id}` });
            const grabbed = summary.grabbed || 0;
            toast(grabbed ? `Watch grabbed ${grabbed} torrent${grabbed === 1 ? '' : 's'}.` : 'Nothing new to grab.', grabbed ? 'success' : 'secondary');
            await loadWatches();
        } catch (error) {
            toast(error.message || 'Run failed.', 'danger');
        } finally {
            setBusy(button, false);
        }
    }

    async function markAllSeen(button) {
        const watchId = state.lastResultWatchId;
        const summary = state.lastResult;
        if (!watchId || !summary) return;
        const items = (summary.items || []).filter(item => item.matched && !item.seen).map(item => ({ id: item.id, title: item.title }));
        if (!items.length) return;
        if (!window.confirm(`Mark ${items.length} matched result${items.length === 1 ? '' : 's'} as seen? They will never be grabbed by this watch.`)) return;
        setBusy(button, true, 'Marking…');
        try {
            const result = await api(`/${watchId}/seen`, { method: 'POST', body: { items } });
            toast(result.message || 'Marked as seen.', 'success');
            await testWatch(watchId, null);
        } catch (error) {
            toast(error.message || 'Unable to mark as seen.', 'danger');
        } finally {
            setBusy(button, false);
        }
    }

    // ------------------------------------------------------------------ events
    const KIND_BADGES = {
        checked: 'text-bg-secondary',
        matched: 'text-bg-info',
        grabbed: 'text-bg-success',
        skipped_seen: 'text-bg-light',
        skipped_cap: 'text-bg-warning',
        error: 'text-bg-danger',
    };

    async function loadEvents(id) {
        state.eventsWatchId = id;
        const watch = state.watches.find(w => String(w.id) === String(id));
        $('watch-events-title').textContent = `History: ${watch?.name || id}`;
        const list = $('watch-events-list');
        list.innerHTML = '';
        try {
            const data = await api(`/${id}/events?limit=50`);
            const events = data.events || [];
            $('watch-events-empty').classList.toggle('d-none', events.length > 0);
            events.forEach(event => {
                const li = document.createElement('li');
                li.className = 'list-group-item px-0';
                li.innerHTML = `
                  <div class="d-flex justify-content-between gap-2">
                    <span><span class="badge ${KIND_BADGES[event.kind] || 'text-bg-secondary'} me-2">${escapeHtml(event.kind.replace('_', ' '))}</span>${escapeHtml(event.title || '')}</span>
                    <span class="text-body-secondary text-nowrap" title="${escapeHtml(formatTimestamp(event.ts))}">${escapeHtml(relativeTime(event.ts))}</span>
                  </div>
                  ${event.detail ? `<div class="text-body-secondary" style="font-size:.8rem;">${escapeHtml(event.detail)}</div>` : ''}`;
                list.appendChild(li);
            });
            showView('watch-events-view');
        } catch (error) {
            toast(error.message || 'Unable to load history.', 'danger');
        }
    }

    // ------------------------------------------------------------------ "Save as watch"
    function readSearchFormAsWatch() {
        const form = document.getElementById('search-form');
        if (!form) return null;
        const fd = new FormData(form);
        const query = String(fd.get('query') || '').trim();
        const fields = ['title', 'author', 'series', 'narrator', 'description', 'tags', 'filenames']
            .filter(name => fd.get(`search_in_${name}`));
        const minSeeders = String(fd.get('min_seeders') || '').trim();
        return {
            name: query,
            query,
            title_regex: query ? `^${escapeRegex(query)}` : '',
            search_fields: fields.length ? fields : ['title'],
            language_ids: fd.getAll('language_ids').map(String).filter(Boolean),
            main_cat: fd.getAll('main_cat').map(String).filter(v => v && v !== 'all'),
            category_ids: fd.getAll('category_ids').map(String).filter(Boolean),
            flag_ids: fd.getAll('flag_ids').map(String).filter(Boolean),
            flags_mode: String(fd.get('flags_mode') || '0'),
            search_type: String(fd.get('searchType') || 'all'),
            min_seeders: minSeeders === '' ? '' : Number(minSeeders),
        };
    }

    async function saveCurrentSearchAsWatch() {
        const prefill = readSearchFormAsWatch();
        if (!prefill) return;
        const advanced = document.getElementById('advancedSearchOffcanvas');
        if (advanced && window.bootstrap) bootstrap.Offcanvas.getInstance(advanced)?.hide();
        const panel = $('watchesOffcanvas');
        if (panel && window.bootstrap) bootstrap.Offcanvas.getOrCreateInstance(panel).show();
        await openForm(null, prefill);
    }

    // ------------------------------------------------------------------ wiring
    function bind() {
        const panel = $('watchesOffcanvas');
        if (!panel) return;

        panel.addEventListener('show.bs.offcanvas', () => {
            loadStatus();
            loadWatches();
        });

        $('watches-refresh')?.addEventListener('click', () => { loadStatus(); loadWatches(); });
        $('watches-new')?.addEventListener('click', () => openForm(null));

        panel.addEventListener('click', (event) => {
            const button = event.target.closest('[data-watch-action]');
            if (!button) return;
            const action = button.dataset.watchAction;
            const id = button.dataset.watchId;
            switch (action) {
                case 'back': showView('watches-list-view'); loadWatches(); break;
                case 'test': testWatch(id, button); break;
                case 'run': runWatch(id, button); break;
                case 'events': loadEvents(id); break;
                case 'edit': openForm(state.watches.find(w => String(w.id) === String(id)) || null); break;
                case 'delete': deleteWatch(id); break;
                default: break;
            }
        });

        panel.addEventListener('change', (event) => {
            const toggle = event.target.closest('.watch-enabled-toggle');
            if (toggle) toggleEnabled(toggle.dataset.watchId, toggle.checked);
        });

        $('watch-form')?.addEventListener('submit', (event) => {
            event.preventDefault();
            saveWatch();
        });
        $('watch-test-form')?.addEventListener('click', (event) => testForm(event.currentTarget));
        $('watch-delete')?.addEventListener('click', () => {
            const id = $('watch-id').value;
            if (id) deleteWatch(id);
        });
        $('watch-regex-from-query')?.addEventListener('click', () => {
            const query = $('watch-query').value.trim();
            if (query) $('watch-title-regex').value = `^${escapeRegex(query)}`;
        });

        $('watch-result-mark-seen')?.addEventListener('click', (event) => markAllSeen(event.currentTarget));
        $('watch-result-retest')?.addEventListener('click', (event) => {
            if (state.lastResultWatchId) testWatch(state.lastResultWatchId, event.currentTarget);
            else testForm(event.currentTarget);
        });
        $('watch-result-run')?.addEventListener('click', (event) => {
            if (state.lastResultWatchId) runWatch(state.lastResultWatchId, event.currentTarget);
        });
        $('watch-events-refresh')?.addEventListener('click', () => {
            if (state.eventsWatchId) loadEvents(state.eventsWatchId);
        });

        document.getElementById('save-as-watch-button')?.addEventListener('click', saveCurrentSearchAsWatch);

        // Keep the nav badge current without opening the panel.
        loadStatus();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', bind);
    } else {
        bind();
    }
})();
