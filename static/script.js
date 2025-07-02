document.addEventListener('DOMContentLoaded', () => {
    // --- Глобальные константы и переменные ---
    const API_BASE_URL = '/api/v1';

    // --- Элементы DOM ---
    const tabButtons = document.querySelectorAll('.tab-button');
    const tabPanes = document.querySelectorAll('.tab-pane');
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
    const directVideoInfo = document.getElementById('directVideoInfo');
    // ИЗМЕНЕНО: Получаем новую кнопку
    const copyPageUrlButton = document.getElementById('copyPageUrlButton');

    // --- Умная функция копирования с fallback'ом ---
    async function copyToClipboard(text, successMessage) {
        if (navigator.clipboard && window.isSecureContext) {
            try {
                await navigator.clipboard.writeText(text);
                alert(successMessage);
            } catch (err) {
                console.error('Ошибка при копировании через Clipboard API: ', err);
                alert('Не удалось скопировать ссылку.');
            }
        } else {
            const textArea = document.createElement("textarea");
            textArea.value = text;
            textArea.style.position = "absolute";
            textArea.style.left = "-9999px";
            document.body.prepend(textArea);
            textArea.select();
            try {
                document.execCommand('copy');
                alert(successMessage);
            } catch (err) {
                console.error('Ошибка при копировании через execCommand: ', err);
                alert('Не удалось скопировать ссылку.');
            } finally {
                textArea.remove();
            }
        }
    }

    // --- Универсальная функция для API запросов ---
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
            alert(`Ошибка: ${error.message}`);
            throw error;
        }
    }

    // --- Логика переключения вкладок ---
    function setupTabs() {
        tabButtons.forEach(button => {
            button.addEventListener('click', () => {
                tabButtons.forEach(btn => btn.classList.remove('active'));
                tabPanes.forEach(pane => pane.classList.remove('active'));
                button.classList.add('active');
                document.getElementById(button.dataset.tab).classList.add('active');
            });
        });
    }

    // --- Логика для вкладки "Live Stream / Очередь" ---
    const liveStreamModule = {
        init() {
            const permanentStreamUrl = `${window.location.origin}${API_BASE_URL}/live_stream`;
            permanentStreamLinkInput.value = permanentStreamUrl;
            liveVideoPlayer.src = permanentStreamUrl;
            addVideoButton.addEventListener('click', this.addVideoToQueue.bind(this));
            nextButton.addEventListener('click', this.playNext.bind(this));
            prevButton.addEventListener('click', this.playPrevious.bind(this));
            copyStreamLinkButton.addEventListener('click', () => {
                copyToClipboard(permanentStreamLinkInput.value, 'Ссылка на постоянный стрим скопирована!');
            });
            this.updateLiveStreamTab();
            setInterval(() => this.updateLiveStreamTab(), 5000);
        },
        async addVideoToQueue() {
            const url = videoUrlInput.value.trim();
            if (!url) return alert('Пожалуйста, введите URL видео.');
            try {
                await fetchApi('/video/add', 'POST', { url });
                videoUrlInput.value = '';
                await this.updateLiveStreamTab();
            } catch (error) {}
        },
        async playNext() { try { await fetchApi('/video/play_next', 'POST'); await this.updateLiveStreamTab(); } catch (error) {} },
        async playPrevious() { try { await fetchApi('/video/play_previous', 'POST'); await this.updateLiveStreamTab(); } catch (error) {} },
        renderQueue(queue, currentVideoIdInQueue) {
            videoQueueList.innerHTML = '';
            if (!queue || queue.length === 0) {
                videoQueueList.innerHTML = '<li class="placeholder">Очередь пуста</li>';
                return;
            }
            queue.forEach(video => {
                const li = document.createElement('li');
                if (video.id_in_queue === currentVideoIdInQueue) li.classList.add('active-in-queue');
                li.innerHTML = `<span class="video-title" title="${video.title || video.original_url}">${video.title || 'Загрузка...'}</span><span class="video-status">(${video.status})</span>`;
                videoQueueList.appendChild(li);
            });
        },
        async updateLiveStreamTab() {
            try {
                const queueState = await fetchApi('/queue');
                const { queue, current_video_id_in_queue } = queueState;
                this.renderQueue(queue, current_video_id_in_queue);
                const lastPlayedId = liveVideoPlayer.dataset.lastPlayedId;
                let currentVideo = current_video_id_in_queue ? queue.find(v => v.id_in_queue === current_video_id_in_queue) : null;
                if (currentVideo) {
                    currentTitleDisplay.textContent = currentVideo.title || 'Загрузка...';
                    currentStatusDisplay.textContent = currentVideo.status;
                    if (currentVideo.id_in_queue !== lastPlayedId) {
                        liveVideoPlayer.dataset.lastPlayedId = currentVideo.id_in_queue;
                        liveVideoPlayer.load();
                    }
                } else {
                    currentTitleDisplay.textContent = 'Stream Offline';
                    currentStatusDisplay.textContent = 'Очередь пуста';
                    if ('placeholder' !== lastPlayedId) {
                        liveVideoPlayer.dataset.lastPlayedId = 'placeholder';
                        liveVideoPlayer.load();
                    }
                }
            } catch (error) {}
        }
    };

    // --- Логика для вкладки "Прямой Прокси" ---
    const directProxyModule = {
        init() {
            loadDirectVideoButton.addEventListener('click', this.loadVideo.bind(this));
            copyDirectStreamLinkButton.addEventListener('click', this.copyStreamLink.bind(this));
            // ИЗМЕНЕНО: Добавляем обработчик для новой кнопки
            copyPageUrlButton.addEventListener('click', this.copyPageUrl);
        },
        // ИЗМЕНЕНО: Новая функция для копирования URL страницы
        copyPageUrl() {
            copyToClipboard(window.location.href, 'URL текущей страницы скопирован!');
        },
        async copyStreamLink() {
            const videoId = directVideoPlayer.dataset.videoId;
            if (!videoId) {
                alert("Сначала загрузите видео, чтобы получить ссылку на стрим.");
                return;
            }
            const streamUrl = `${window.location.origin}${API_BASE_URL}/stream_remux/${videoId}?chunk=0`;
            copyToClipboard(streamUrl, 'Ссылка на прокси-стрим (первый чанк) скопирована!');
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
            directVideoPlayer.removeAttribute('src');
            directVideoPlayer.dataset.videoId = '';
            if (!videoId) {
                alert('Не удалось извлечь Video ID. Проверьте URL или вставьте ID напрямую.');
                return;
            }
            const streamUrl = `${API_BASE_URL}/stream_remux/${videoId}?chunk=0`;
            directVideoPlayer.src = streamUrl;
            directVideoPlayer.dataset.videoId = videoId;
            directVideoPlayer.load();
            directVideoPlayer.play().catch(e => console.warn("Autoplay was prevented.", e));
            directVideoInfo.innerHTML = `<p>Загрузка видео с ID: <strong>${videoId}</strong> (используя yt-dlp/ffmpeg)</p>`;
            directVideoPlayer.onerror = () => {
                directVideoInfo.innerHTML = `<p style="color: #ff6b6b;">Ошибка при загрузке видео. Проверьте консоль бэкенда на наличие ошибок.</p>`;
            };
            directVideoPlayer.oncanplay = () => {
                directVideoInfo.innerHTML = `<p>Воспроизводится первый 10-секундный чанк видео с ID: <strong>${videoId}</strong></p>`;
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