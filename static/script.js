document.addEventListener('DOMContentLoaded', () => {
    // --- Глобальные константы и переменные ---
    const API_BASE_URL = '/api/v1';

    // --- Элементы DOM ---
    const navButtons = document.querySelectorAll('.nav-button');
    const tabPanes = document.querySelectorAll('.tab-pane');
    const notificationContainer = document.getElementById('notification-container');
    const actionButtons = document.querySelector('.action-buttons'); // ИЗМЕНЕНО: Получаем контейнер кнопок
    const liveVideoPlayer = document.getElementById('liveVideoPlayer');
    const permanentStreamLinkInput = document.getElementById('permanentStreamLink');
    const copyStreamLinkButton = document.getElementById('copyStreamLinkButton');
    const currentTitleDisplay = document.getElementById('currentTitle');
    const currentStatusDisplay = document.getElementById('currentStatus');
    const videoUrlInput = document.getElementById('videoUrl');
    const addVideoButton = document.getElementById('addVideoButton');
    const prevButton = document.getElementById('prevButton');
    const nextButton = document.getElementById('nextButton');
    const videoQueueList = document.getElementById('videoQueueList');
    const directVideoUrlInput = document.getElementById('directVideoUrl');
    const loadDirectVideoButton = document.getElementById('loadDirectVideoButton');
    const copyDirectStreamLinkButton = document.getElementById('copyDirectStreamLinkButton');
    const directVideoPlayer = document.getElementById('directVideoPlayer');
    const directPlayerWrapper = document.querySelector('.player-wrapper');
    const directVideoInfo = document.getElementById('directVideoInfo');
    const copyPageUrlButton = document.getElementById('copyPageUrlButton');

    // --- Система уведомлений (Toast) ---
    function showToast(message, type = 'info', duration = 4000) {
        const toast = document.createElement('div');
        toast.className = `toast ${type}`;
        toast.textContent = message;
        notificationContainer.appendChild(toast);
        setTimeout(() => { toast.classList.add('show'); }, 10);
        setTimeout(() => {
            toast.classList.remove('show');
            toast.addEventListener('transitionend', () => { toast.remove(); });
        }, duration);
    }

    // --- Функция копирования ---
    async function copyToClipboard(text, successMessage) {
        if (navigator.clipboard && window.isSecureContext) {
            try {
                await navigator.clipboard.writeText(text);
                showToast(successMessage, 'info');
            } catch (err) { showToast('Не удалось скопировать ссылку.', 'error'); }
        } else {
            const textArea = document.createElement("textarea");
            textArea.value = text;
            textArea.style.position="absolute"; textArea.style.left="-9999px";
            document.body.prepend(textArea);
            textArea.select();
            try {
                document.execCommand('copy');
                showToast(successMessage, 'info');
            } catch (err) { showToast('Не удалось скопировать ссылку.', 'error'); }
            finally { textArea.remove(); }
        }
    }

    // --- API запросы ---
    async function fetchApi(endpoint, method = 'GET', body = null) {
        const options = { method, headers: { 'Content-Type': 'application/json' } };
        if (body) options.body = JSON.stringify(body);
        try {
            const response = await fetch(`${API_BASE_URL}${endpoint}`, options);
            if (!response.ok) {
                const errorData = await response.json().catch(() => ({ detail: response.statusText }));
                throw new Error(`API Error (${response.status}): ${errorData.detail}`);
            }
            if (response.status === 204 || response.headers.get("content-length") === "0") return null;
            return response.json();
        } catch (error) {
            console.error('Fetch API error:', error);
            showToast(`Ошибка: ${error.message}`, 'error');
            throw error;
        }
    }

    // --- Логика переключения вкладок ---
    function setupTabs() {
        navButtons.forEach(button => {
            button.addEventListener('click', () => {
                navButtons.forEach(btn => btn.classList.remove('active'));
                tabPanes.forEach(pane => pane.classList.remove('active'));
                button.classList.add('active');
                document.getElementById(button.dataset.tab).classList.add('active');
            });
        });
    }

    // --- Логика для вкладки "Live Stream / Очередь" ---
    const liveStreamModule = {
        init() {
            if (!addVideoButton) return;
            // ... (остальной код модуля без изменений)
        },
        // ...
    };

    // --- Логика для вкладки "Прямой Прокси" ---
    const directProxyModule = {
        init() {
            if (!loadDirectVideoButton) return;
            loadDirectVideoButton.addEventListener('click', this.loadVideo.bind(this));
            copyDirectStreamLinkButton.addEventListener('click', this.copyStreamLink.bind(this));
            copyPageUrlButton.addEventListener('click', this.copyPageUrl);
        },
        copyPageUrl() {
            copyToClipboard(window.location.href, 'URL текущей страницы скопирован!');
        },
        async copyStreamLink() {
            const videoId = directVideoPlayer.dataset.videoId;
            if (!videoId) {
                showToast("Сначала загрузите видео, чтобы получить ссылку.", 'error');
                return;
            }
            const streamUrl = `${window.location.origin}${API_BASE_URL}/stream_remux/${videoId}?chunk=0`;
            copyToClipboard(streamUrl, 'Ссылка на прокси-стрим скопирована!');
        },
        extractVideoId(url) {
            if (!url) return null;
            if (/^[a-zA-Z0-9_-]{11}$/.test(url)) return url;
            const regex = /(?:v=|youtu\.be\/|embed\/|watch\?v=|\/v\/)([^&\s?]+)/;
            const match = url.match(regex);
            return match ? match[1] : null;
        },
        async loadVideo() {
            const input = directVideoUrlInput.value.trim();
            const videoId = this.extractVideoId(input);

            // ИЗМЕНЕНО: Сброс состояния и скрытие кнопок
            directPlayerWrapper.classList.remove('visible');
            actionButtons.classList.add('hidden');
            directVideoPlayer.removeAttribute('src');
            directVideoPlayer.dataset.videoId = '';

            if (!videoId) {
                showToast('Не удалось извлечь Video ID. Проверьте ссылку.', 'error');
                return;
            }

            const streamUrl = `${API_BASE_URL}/stream_remux/${videoId}?chunk=0`;
            directVideoPlayer.src = streamUrl;
            directVideoPlayer.dataset.videoId = videoId;

            directPlayerWrapper.classList.add('visible');
            directVideoInfo.innerHTML = `<p>Загрузка видео с ID: <strong>${videoId}</strong>...</p>`;
            
            directVideoPlayer.load();
            directVideoPlayer.play().catch(e => console.warn("Autoplay was prevented.", e));

            directVideoPlayer.onerror = () => {
                directVideoInfo.innerHTML = `<p style="color: var(--error-color);">Ошибка при загрузке видео. Проверьте консоль бэкенда.</p>`;
                actionButtons.classList.add('hidden'); // Скрываем кнопки при ошибке
            };
            directVideoPlayer.oncanplay = () => {
                 directVideoInfo.innerHTML = `<p>Воспроизводится видео с ID: <strong>${videoId}</strong></p>`;
                 actionButtons.classList.remove('hidden'); // ИЗМЕНЕНО: Показываем кнопки, когда видео готово
            };
        }
    };

    function main() {
        setupTabs();
        liveStreamModule.init();
        directProxyModule.init();
    }

    main();
});