/* Wiki search and the contents toggle. Plain ES5, no build step. */

/* Contents toggle */
(function () {
    document.addEventListener('DOMContentLoaded', function () {
        var toggle = document.querySelector('.wiki-nav-toggle');
        var sidebar = document.getElementById('wiki-sidebar');
        if (!toggle || !sidebar) return;
        toggle.addEventListener('click', function () {
            var open = sidebar.classList.toggle('open');
            toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
        });
    });
})();

/* Wiki search */
(function () {
    document.addEventListener('DOMContentLoaded', function () {
        var input = document.querySelector('.search-input');
        var resultsContainer = document.querySelector('.search-results');
        if (!input || !resultsContainer) return;

        var index = null;
        var debounceTimer = null;
        var activeIdx = -1;
        var DEBOUNCE_MS = 200;
        var MIN_QUERY = 2;

        var indexUrl = '/wiki/search-index.json';
        var linkPrefix = '/wiki/';

        function loadIndex(cb) {
            if (index) { cb(); return; }
            fetch(indexUrl)
                .then(function (r) { return r.json(); })
                .then(function (data) { index = data; cb(); })
                .catch(function () { index = []; cb(); });
        }

        function search(query) {
            var words = query.toLowerCase().trim().split(/\s+/);
            var results = [];
            for (var i = 0; i < index.length; i++) {
                var entry = index[i];
                var haystack = (entry.pageTitle + ' ' + entry.section + ' ' + entry.text).toLowerCase();
                var allMatch = true;
                for (var w = 0; w < words.length; w++) {
                    if (haystack.indexOf(words[w]) === -1) { allMatch = false; break; }
                }
                if (allMatch) results.push(entry);
            }
            return results.slice(0, 8);
        }

        function escapeHtml(str) {
            var d = document.createElement('div');
            d.textContent = str;
            return d.innerHTML;
        }

        function buildSnippet(text, query) {
            var words = query.toLowerCase().trim().split(/\s+/);
            var lower = text.toLowerCase();
            var firstPos = text.length;
            for (var w = 0; w < words.length; w++) {
                var pos = lower.indexOf(words[w]);
                if (pos !== -1 && pos < firstPos) firstPos = pos;
            }
            var start = Math.max(0, firstPos - 40);
            var end = Math.min(text.length, firstPos + 120);
            var snippet = (start > 0 ? '…' : '') + text.substring(start, end) + (end < text.length ? '…' : '');
            snippet = escapeHtml(snippet);
            for (var w = 0; w < words.length; w++) {
                var escaped = words[w].replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
                snippet = snippet.replace(new RegExp('(' + escaped + ')', 'gi'), '<mark>$1</mark>');
            }
            return snippet;
        }

        function renderResults(results, query) {
            if (results.length === 0) {
                resultsContainer.innerHTML = '<div class="search-no-results">No results found</div>';
                resultsContainer.classList.add('open');
                activeIdx = -1;
                return;
            }
            var html = '';
            for (var i = 0; i < results.length; i++) {
                var r = results[i];
                var href = linkPrefix + r.page + (r.sectionId ? '#' + r.sectionId : '');
                html += '<a class="search-result" href="' + href + '" role="option">';
                html += '<div class="search-result-title">' + escapeHtml(r.pageTitle) + '</div>';
                if (r.section) html += '<div class="search-result-section">' + escapeHtml(r.section) + '</div>';
                html += '<div class="search-result-snippet">' + buildSnippet(r.text, query) + '</div>';
                html += '</a>';
            }
            resultsContainer.innerHTML = html;
            resultsContainer.classList.add('open');
            activeIdx = -1;
        }

        function closeResults() {
            resultsContainer.classList.remove('open');
            resultsContainer.innerHTML = '';
            activeIdx = -1;
        }

        function getLinks() { return resultsContainer.querySelectorAll('.search-result'); }

        function updateActive() {
            var links = getLinks();
            for (var i = 0; i < links.length; i++) {
                links[i].classList.toggle('active', i === activeIdx);
            }
            if (activeIdx >= 0 && links[activeIdx]) {
                links[activeIdx].scrollIntoView({ block: 'nearest' });
            }
        }

        input.addEventListener('input', function () {
            clearTimeout(debounceTimer);
            var q = input.value;
            if (q.length < MIN_QUERY) { closeResults(); return; }
            debounceTimer = setTimeout(function () {
                loadIndex(function () { renderResults(search(q), q); });
            }, DEBOUNCE_MS);
        });

        input.addEventListener('focus', function () {
            loadIndex(function () {});
            if (input.value.length >= MIN_QUERY) {
                loadIndex(function () { renderResults(search(input.value), input.value); });
            }
        });

        input.addEventListener('keydown', function (e) {
            var links = getLinks();
            if (!resultsContainer.classList.contains('open') || links.length === 0) {
                if (e.key === 'Escape') input.blur();
                return;
            }
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                activeIdx = Math.min(activeIdx + 1, links.length - 1);
                updateActive();
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                activeIdx = Math.max(activeIdx - 1, -1);
                updateActive();
            } else if (e.key === 'Enter') {
                e.preventDefault();
                if (activeIdx >= 0 && links[activeIdx]) links[activeIdx].click();
            } else if (e.key === 'Escape') {
                closeResults();
                input.blur();
            }
        });

        document.addEventListener('click', function (e) {
            if (!e.target.closest('.wiki-sidebar')) closeResults();
        });
    });
})();
