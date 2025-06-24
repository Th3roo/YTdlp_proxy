document.addEventListener('DOMContentLoaded', () => {
    const videoUrlInput = document.getElementById('videoUrl');
    const addVideoButton = document.getElementById('addVideoButton');
    const prevButton = document.getElementById('prevButton');
    const pauseResumeButton = document.getElementById('pauseResumeButton');
    const nextButton = document.getElementById('nextButton');
    const videoQueueList = document.getElementById('videoQueueList');

    // Элементы для плеера и постоянной ссылки
    const videoPlayer = document.getElementById('videoPlayer');
    const permanentStreamLinkInput = document.getElementById('permanentStreamLink');
    const copyStreamLinkButton = document.getElementById('copyStreamLinkButton');

    const currentTitleDisplay = document.getElementById('currentTitle');
    // const currentLinkDisplay = document.getElementById('currentLink'); // Больше не используется напрямую
    const currentStatusDisplay = document.getElementById('currentStatus');

    const API_BASE_URL = '/api/v1';
    const PERMANENT_STREAM_URL = `${window.location.origin}${API_BASE_URL}/live_stream`;

    // --- Функции для взаимодействия с API ---

    async function fetchApi(endpoint, method = 'GET', body = null) {
        const options = {
            method,
            headers: {
                'Content-Type': 'application/json',
            },
        };
        if (body) {
            options.body = JSON.stringify(body);
        }
        try {
            const response = await fetch(`${API_BASE_URL}${endpoint}`, options);
            if (!response.ok) {
                const errorData = await response.json().catch(() => ({ detail: response.statusText }));
                throw new Error(`API Error (${response.status}): ${errorData.detail}`);
            }
            // Для DELETE запросов или других, которые могут не возвращать JSON
            if (response.status === 204 || response.headers.get("content-length") === "0") {
                return null;
            }
            return await response.json();
        } catch (error) {
            console.error('Fetch API error:', error);
            alert(`Ошибка: ${error.message}`);
            throw error;
        }
    }

    async function addVideo(url) {
        try {
            const data = await fetchApi('/video/add', 'POST', { url });
            console.log('Video added:', data);
            await refreshQueue();
            await updateCurrentVideoDisplay(); // Обновить инфо о текущем видео
            videoUrlInput.value = ''; // Очистить поле ввода
        } catch (error) {
            // Ошибка уже обработана в fetchApi
        }
    }

    async function getQueueState() { // Переименована для ясности, что получаем весь объект ответа
        try {
            const data = await fetchApi('/queue');
            return data; // Ожидаем объект { queue: [], current_video_id_in_queue: "...", total_items: 0 }
        } catch (error) {
            return { queue: [], current_video_id_in_queue: null, total_items: 0 };
        }
    }

    async function getCurrentVideoDetails() { // Переименована для ясности
        try {
            const data = await fetchApi('/current_video_link');
            return data.video_info || null;
        } catch (error) {
            if (error.message.includes("404")) {
                return null;
            }
            console.error('Error fetching current video details:', error);
            return null;
        }
    }

    async function triggerDownload(videoIdInQueue) {
        try {
            const data = await fetchApi(`/video/${videoIdInQueue}/download`, 'POST');
            console.log('Download initiated:', data);
            alert(data.message || `Запрос на скачивание видео ${videoIdInQueue} отправлен.`);
            await refreshQueueAndCurrentVideo(); // Обновляем очередь и текущее видео
        } catch (error) {
            // Ошибка уже обработана в fetchApi
        }
    }

    async function cancelDownload(videoIdInQueue) {
        try {
            const data = await fetchApi(`/video/${videoIdInQueue}/cancel_download`, 'POST');
            console.log('Download cancellation processed:', data);
            alert(data.message || `Запрос на отмену скачивания для видео ${videoIdInQueue} обработан.`);
            await refreshQueueAndCurrentVideo(); // Обновляем очередь и текущее видео
        } catch (error) {
            // Ошибка уже обработана в fetchApi
        }
    }

    async function playNext() {
        try {
            await fetchApi('/video/play_next', 'POST');
            // refreshQueueAndCurrentVideo() будет вызван периодическим обновлением
            // или мы можем вызвать его принудительно, если хотим немедленной реакции.
            // Принудительный вызов лучше для UX.
            await refreshQueueAndCurrentVideo();
            // После обновления информации, если видео реально сменилось, плеер должен был получить .load()
            // Дополнительно можно попробовать запустить воспроизведение, если оно не началось само.
            // videoPlayer.play().catch(e => console.warn("Autoplay prevented on next:", e));
        } catch (error) {
            // Ошибка уже обработана в fetchApi
        }
    }

    async function playPrevious() {
        try {
            await fetchApi('/video/play_previous', 'POST');
            await refreshQueueAndCurrentVideo();
            // videoPlayer.play().catch(e => console.warn("Autoplay prevented on prev:", e));
        } catch (error) {
            // Ошибка уже обработана в fetchApi
        }
    }

    async function togglePauseResume() {
        // Теперь управляем только локальным плеером
        if (videoPlayer.paused || videoPlayer.ended) {
            videoPlayer.play().catch(e => {
                console.error("Error trying to play video:", e);
                alert("Не удалось запустить воспроизведение. Возможно, видео еще не загрузилось или произошла ошибка.");
            });
            // pauseResumeButton.textContent = 'Пауза'; // Обновим текст кнопки
        } else {
            videoPlayer.pause();
            // pauseResumeButton.textContent = 'Старт'; // Обновим текст кнопки
        }
        // Вызов API /video/pause_resume больше не нужен для фронтенда,
        // но если он используется для внешней синхронизации, его можно оставить.
        // Пока уберем его для чистоты управления плеером с фронта.
        /*
        try {
            // const data = await fetchApi('/video/pause_resume', 'POST');
            // console.log('Pause/Resume API Toggled:', data.message);
            // await refreshQueueAndCurrentVideo();
        } catch (error) {
            // console.error("Error calling pause/resume API:", error);
        }
        */
    }

    // Обновление текста кнопки Пауза/Старт в зависимости от состояния плеера
    videoPlayer.onplay = () => { pauseResumeButton.textContent = 'Пауза'; };
    videoPlayer.onpause = () => { pauseResumeButton.textContent = 'Старт'; };
    videoPlayer.onended = () => { pauseResumeButton.textContent = 'Старт'; };


    // --- Функции для обновления UI ---

    function renderQueue(queue, currentVideoIdInQueue) {
        videoQueueList.innerHTML = '';
        if (!queue || queue.length === 0) {
            const placeholder = document.createElement('li');
            placeholder.className = 'placeholder';
            placeholder.textContent = 'Очередь пуста';
            videoQueueList.appendChild(placeholder);
            return;
        }

        queue.forEach((video) => { // index не нужен напрямую
            const listItem = document.createElement('li');

            const titleSpan = document.createElement('span');
            titleSpan.className = 'video-title';
            titleSpan.textContent = video.title || video.original_url || 'Загрузка метаданных...';
            titleSpan.title = video.title || video.original_url;

            const statusSpan = document.createElement('span');
            statusSpan.className = 'video-status';
            let statusText = `(${video.status || 'N/A'})`;
            if (video.status === "metadata_failed" || video.status === "download_failed") {
                if (video.error_message) {
                    statusSpan.title = video.error_message; // Всплывающая подсказка с ошибкой
                    statusText += " (i)"; // Индикатор, что есть доп. инфо
                }
            }
            statusSpan.textContent = statusText;

            listItem.appendChild(titleSpan);

            // Кнопка Скачать/Статус скачивания
            if (video.status === "metadata_fetched" || video.status === "download_failed" || video.status === "pending_metadata") {
                const downloadButton = document.createElement('button');
                downloadButton.textContent = (video.status === "download_failed") ? 'Повторить скачивание' : 'Скачать';
                downloadButton.className = 'download-btn';
                downloadButton.onclick = () => triggerDownload(video.id_in_queue);
                listItem.appendChild(downloadButton);
            } else if (video.status === "pending_download" || video.status === "downloading") {
                 const downloadingSpan = document.createElement('span');
                 downloadingSpan.className = 'downloading-indicator';
                 downloadingSpan.textContent = 'Скачивается...';
                 listItem.appendChild(downloadingSpan);

                const cancelButton = document.createElement('button');
                cancelButton.textContent = 'Отменить загрузку';
                cancelButton.className = 'cancel-download-btn'; // Новый класс для стилизации
                cancelButton.onclick = () => cancelDownload(video.id_in_queue);
                listItem.appendChild(cancelButton);

            } else if (video.status === "downloaded") {
                const downloadedSpan = document.createElement('span');
                downloadedSpan.className = 'downloaded-indicator';
                downloadedSpan.textContent = 'Скачано';
                listItem.appendChild(downloadedSpan);
            }

            listItem.appendChild(statusSpan);

            if (video.id_in_queue && video.id_in_queue === currentVideoIdInQueue) {
                listItem.classList.add('active-in-queue');
            }
            videoQueueList.appendChild(listItem);
        });
    }

    async function updateCurrentVideoDisplay() {
        const currentVideo = await getCurrentVideoDetails();
        if (currentVideo) {
            currentTitleDisplay.textContent = currentVideo.title || currentVideo.original_url || 'N/A';
            // Предполагаем, что если видео скачано, то ссылка будет на локальный файл,
            // иначе - на оригинальный URL. Это нужно будет доработать в API.
            const link = currentVideo.downloaded_path || currentVideo.original_url || '#';
            currentLinkDisplay.href = link;
            currentLinkDisplay.textContent = link !== '#' ? (currentVideo.downloaded_path ? 'Локальный файл' : 'Открыть источник') : '-';
            currentStatusDisplay.textContent = currentVideo.status || 'N/A';
        } else {
            currentTitleDisplay.textContent = '-';
            currentLinkDisplay.href = '#';
            currentLinkDisplay.textContent = '-';
            currentStatusDisplay.textContent = 'Нет активного видео';
        }
    }

    async function refreshQueueAndCurrentVideo() {
        try {
            const queueState = await getQueueState(); // Получаем состояние очереди
            const queue = queueState.queue || [];
            const currentVideoIdFromQueueState = queueState.current_video_id_in_queue || null;

            renderQueue(queue, currentVideoIdFromQueueState); // Рендерим очередь

            // Обновляем информацию о текущем видео, основываясь на ID из состояния очереди,
            // или запрашиваем отдельно, если ID нет (на случай рассинхрона или первого запуска)
            let currentVideoToDisplay = null;
            if (currentVideoIdFromQueueState) {
                currentVideoToDisplay = queue.find(v => v.id_in_queue === currentVideoIdFromQueueState);
            }

            // Если в данных очереди нет инфо о текущем видео (например, оно было удалено, а ID еще старый)
            // или если currentVideoIdFromQueueState был null, но очередь не пуста,
            // то запросим актуальное текущее видео с /current_video_link.
            // Это также полезно при первой загрузке, когда currentVideoIdFromQueueState может быть не установлен.
            if (!currentVideoToDisplay && queue.length > 0) {
                 // console.log("Current video not found in queue data or ID was null, fetching details separately...");
                 currentVideoToDisplay = await getCurrentVideoDetails();
            } else if (!currentVideoToDisplay && queue.length === 0) {
                // Если очередь пуста, то и текущего видео нет
                currentVideoToDisplay = null;
            }


            // Обновление UI для текущего видео (информационная панель)
            // и управление плеером
            let shouldReloadPlayer = false;
            // Проверяем, изменился ли ID текущего видео по сравнению с тем, что плеер мог играть до этого.
            // Мы можем хранить предыдущий ID в data-атрибуте плеера или в глобальной переменной.
            const previousPlayerVideoId = videoPlayer.dataset.currentVideoIdInQueue;

            if (currentVideoToDisplay) {
                currentTitleDisplay.textContent = currentVideoToDisplay.title || currentVideoToDisplay.original_url || 'Загрузка...';
                currentStatusDisplay.textContent = currentVideoToDisplay.status || 'N/A';

                if (currentVideoToDisplay.id_in_queue !== previousPlayerVideoId) {
                    shouldReloadPlayer = true;
                    videoPlayer.dataset.currentVideoIdInQueue = currentVideoToDisplay.id_in_queue;
                }
            } else { // Нет активного видео (возможно, будет играть заглушка)
                currentTitleDisplay.textContent = 'Нет активного видео (заглушка)';
                currentStatusDisplay.textContent = 'Очередь пуста или видео не выбрано';
                if (previousPlayerVideoId !== 'placeholder') { // Если до этого играло нечто иное, чем заглушка
                    shouldReloadPlayer = true;
                    videoPlayer.dataset.currentVideoIdInQueue = 'placeholder'; // Специальный маркер для заглушки
                }
            }

            // Если ID текущего видео изменился, или если плеер еще не был инициализирован (первый запуск),
            // или если мы перешли с видео на заглушку (или наоборот).
            // `videoPlayer.src` всегда указывает на `/api/v1/live_stream`.
            // `load()` заставит плеер переподключиться и запросить актуальный контент.
            if (shouldReloadPlayer || !videoPlayer.dataset.initialized) {
                console.log("Reloading player source due to current video change or initialization.");
                videoPlayer.load(); // Перезагружаем источник плеера
                // videoPlayer.play().catch(e => console.warn("Autoplay prevented:", e)); // Попытка автовоспроизведения
                videoPlayer.dataset.initialized = "true";
            }

        } catch (error) {
            console.error("Error refreshing queue and current video:", error);
            renderQueue([], null);
            currentTitleDisplay.textContent = 'Ошибка';
            currentStatusDisplay.textContent = 'Не удалось загрузить данные';
            videoPlayer.dataset.currentVideoIdInQueue = 'error'; // Статус ошибки для плеера
        }
    }


    // --- Инициализация и обработчики событий ---

    addVideoButton.addEventListener('click', async () => { // Сделаем async для await
        const url = videoUrlInput.value.trim();
        if (url) {
            await addVideo(url); // addVideo уже вызывает refreshQueue и updateCurrentVideoDisplay
                                 // нужно будет это пересмотреть. Пока что addVideo должен вызывать refreshQueueAndCurrentVideo
        } else {
            alert('Пожалуйста, введите URL видео.');
        }
    });

    // Переделываем addVideo, чтобы он вызывал refreshQueueAndCurrentVideo
    async function addVideo(url) {
        try {
            const data = await fetchApi('/video/add', 'POST', { url });
            console.log('Video added:', data);
            await refreshQueueAndCurrentVideo(); // Единая функция обновления
            videoUrlInput.value = '';
        } catch (error) {
            // Ошибка уже обработана в fetchApi
        }
    }


    nextButton.addEventListener('click', playNext);
    prevButton.addEventListener('click', playPrevious);
    pauseResumeButton.addEventListener('click', togglePauseResume);

    // Первоначальная загрузка данных
    refreshQueueAndCurrentVideo();

    // (Опционально) Периодическое обновление очереди и текущего видео
    setInterval(refreshQueueAndCurrentVideo, 5000); // каждые 5 секунд

    // --- Инициализация плеера и ссылки ---
    function initializePlayerAndLink() {
        permanentStreamLinkInput.value = PERMANENT_STREAM_URL;
        videoPlayer.src = PERMANENT_STREAM_URL;
        // videoPlayer.load(); // Можно вызвать load, чтобы сразу пошла заглушка, если очередь пуста
                           // или это сделает refreshQueueAndCurrentVideo при первом вызове
    }

    copyStreamLinkButton.addEventListener('click', () => {
        permanentStreamLinkInput.select();
        permanentStreamLinkInput.setSelectionRange(0, 99999); // For mobile devices
        try {
            document.execCommand('copy');
            alert('Ссылка на стрим скопирована в буфер обмена!');
        } catch (err) {
            alert('Не удалось скопировать ссылку. Пожалуйста, скопируйте вручную.');
            console.error('Fallback: Oops, unable to copy', err);
        }
        window.getSelection().removeAllRanges(); // Снять выделение
    });

    // Вызов инициализации при загрузке
    initializePlayerAndLink();
});
