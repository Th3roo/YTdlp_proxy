document.addEventListener('DOMContentLoaded', () => {
    const videoUrlInput = document.getElementById('videoUrl');
    const addVideoButton = document.getElementById('addVideoButton');
    const prevButton = document.getElementById('prevButton');
    const pauseResumeButton = document.getElementById('pauseResumeButton');
    const nextButton = document.getElementById('nextButton');
    const videoQueueList = document.getElementById('videoQueueList');

    const currentTitleDisplay = document.getElementById('currentTitle');
    const currentLinkDisplay = document.getElementById('currentLink');
    const currentStatusDisplay = document.getElementById('currentStatus');

    const API_BASE_URL = '/api/v1';

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
            // Обновляем очередь, чтобы отобразить изменение статуса (pending_download, downloading)
            await refreshQueue();
        } catch (error) {
            // Ошибка уже обработана в fetchApi
        }
    }

    async function playNext() {
        try {
            await fetchApi('/video/play_next', 'POST');
            await refreshQueueAndCurrentVideo();
        } catch (error) {
            // Ошибка уже обработана
        }
    }

    async function playPrevious() {
        try {
            await fetchApi('/video/play_previous', 'POST');
            await refreshQueueAndCurrentVideo();
        } catch (error) {
            // Ошибка уже обработана
        }
    }

    async function togglePauseResume() {
        try {
            const data = await fetchApi('/video/pause_resume', 'POST');
            console.log('Pause/Resume Toggled:', data.message);
            alert(data.message);
            await refreshQueueAndCurrentVideo(); // Обновить, если статус меняется
        } catch (error) {
            // Ошибка уже обработана
        }
    }

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
            } else if (video.status === "downloaded") {
                const downloadedSpan = document.createElement('span');
                downloadedSpan.className = 'downloaded-indicator';
                downloadedSpan.textContent = 'Скачано';
                listItem.appendChild(downloadedSpan);
            }

            listItem.appendChild(statusSpan); // Статус теперь в конце или рядом с кнопкой

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


            // Обновление UI для текущего видео
            if (currentVideoToDisplay) {
                currentTitleDisplay.textContent = currentVideoToDisplay.title || currentVideoToDisplay.original_url || 'N/A';
                const link = currentVideoToDisplay.downloaded_path || currentVideoToDisplay.original_url || '#';
                currentLinkDisplay.href = link;
                // Улучшенное отображение текста ссылки
                if (currentVideoToDisplay.downloaded_path) {
                    currentLinkDisplay.textContent = `Локальный файл (${currentVideoToDisplay.downloaded_path.split(/[\\/]/).pop()})`;
                } else if (currentVideoToDisplay.original_url) {
                    currentLinkDisplay.textContent = 'Открыть источник';
                } else {
                    currentLinkDisplay.textContent = '-';
                }
                currentStatusDisplay.textContent = currentVideoToDisplay.status || 'N/A';
            } else {
                currentTitleDisplay.textContent = '-';
                currentLinkDisplay.href = '#';
                currentLinkDisplay.textContent = '-';
                currentStatusDisplay.textContent = 'Нет активного видео';
            }

        } catch (error) {
            console.error("Error refreshing queue and current video:", error);
            renderQueue([], null); // Показать пустую очередь в случае ошибки
            // Сбросить отображение текущего видео
            currentTitleDisplay.textContent = '-';
            currentLinkDisplay.href = '#';
            currentLinkDisplay.textContent = '-';
            currentStatusDisplay.textContent = 'Ошибка загрузки данных';
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
});
