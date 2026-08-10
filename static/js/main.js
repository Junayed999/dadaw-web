document.addEventListener('DOMContentLoaded', () => {
    const analyzeForm = document.getElementById('analyze-form');
    const urlInput = document.getElementById('url-input');
    const analyzeBtn = document.getElementById('analyze-btn');
    const btnText = analyzeBtn.querySelector('.btn-text');
    const btnLoader = analyzeBtn.querySelector('.btn-loader');
    
    const errorMessage = document.getElementById('error-message');
    const resultsSection = document.getElementById('results-section');
    
    const videoThumbnail = document.getElementById('video-thumbnail');
    const videoDuration = document.getElementById('video-duration');
    const videoTitle = document.getElementById('video-title');
    const videoMeta = document.getElementById('video-meta');
    const optionsList = document.getElementById('options-list');
    
    const progressArea = document.getElementById('progress-area');
    const progressStatus = document.getElementById('progress-status');
    const progressPercent = document.getElementById('progress-percent');
    const progressBarFill = document.getElementById('progress-bar-fill');
    const progressSpeed = document.getElementById('progress-speed');
    const progressEta = document.getElementById('progress-eta');
    const downloadSuccess = document.getElementById('download-success');

    let currentUrl = '';

    // Format duration from seconds to MM:SS or HH:MM:SS
    function formatDuration(seconds) {
        if (!seconds) return '';
        const h = Math.floor(seconds / 3600);
        const m = Math.floor((seconds % 3600) / 60);
        const s = Math.floor(seconds % 60);
        
        if (h > 0) {
            return `${h}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
        }
        return `${m}:${s.toString().padStart(2, '0')}`;
    }

    // Set UI state
    function setLoading(isLoading) {
        if (isLoading) {
            btnText.classList.add('hidden');
            btnLoader.classList.remove('hidden');
            analyzeBtn.disabled = true;
            errorMessage.classList.add('hidden');
            resultsSection.classList.add('hidden');
        } else {
            btnText.classList.remove('hidden');
            btnLoader.classList.add('hidden');
            analyzeBtn.disabled = false;
        }
    }

    function showError(msg) {
        errorMessage.textContent = msg;
        errorMessage.classList.remove('hidden');
    }

    // Analyze URL
    analyzeForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const url = urlInput.value.trim();
        if (!url) return;
        
        currentUrl = url;
        setLoading(true);

        try {
            const response = await fetch('/api/analyze', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url })
            });
            
            const data = await response.json();
            
            if (!response.ok) {
                throw new Error(data.error || 'Failed to analyze URL');
            }

            renderResults(data);
            
        } catch (error) {
            showError(error.message);
        } finally {
            setLoading(false);
        }
    });

    // Render analysis results
    function renderResults(data) {
        videoTitle.textContent = data.title;
        videoMeta.textContent = `${data.platform || 'Unknown Source'} • ${data.uploader || 'Unknown Author'}`;
        
        if (data.thumbnail) {
            videoThumbnail.src = data.thumbnail;
        } else {
            videoThumbnail.src = 'data:image/svg+xml;charset=UTF-8,%3Csvg%20width%3D%22200%22%20height%3D%22112%22%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%20200%20112%22%20preserveAspectRatio%3D%22none%22%3E%3Cdefs%3E%3Cstyle%20type%3D%22text%2Fcss%22%3E%23holder_18991461f67%20text%20%7B%20fill%3A%2394a3b8%3Bfont-weight%3A500%3Bfont-family%3AInter%2C%20sans-serif%2C%20monospace%3Bfont-size%3A10pt%20%7D%20%3C%2Fstyle%3E%3C%2Fdefs%3E%3Cg%20id%3D%22holder_18991461f67%22%3E%3Crect%20width%3D%22200%22%20height%3D%22112%22%20fill%3D%22%23f1f5f9%22%3E%3C%2Frect%3E%3Cg%3E%3Ctext%20x%3D%2272.5%22%20y%3D%2260%22%3ENo%20Image%3C%2Ftext%3E%3C%2Fg%3E%3C%2Fg%3E%3C%2Fsvg%3E';
        }
        
        if (data.duration) {
            videoDuration.textContent = formatDuration(data.duration);
            videoDuration.classList.remove('hidden');
        } else {
            videoDuration.classList.add('hidden');
        }

        optionsList.innerHTML = '';
        
        // Add Best Quality option
        addOptionRow('Best Available', 'MP4', 'Video + Audio', 'primary');
        
        // Add specific resolutions
        data.resolutions.forEach(res => {
            if (res !== 'Best Available') {
                addOptionRow(`${res}p`, 'MP4', 'Video + Audio');
            }
        });
        
        // Add Audio Only
        addOptionRow('Audio Only', 'MP3', 'Audio track only', 'audio');

        resultsSection.classList.remove('hidden');
        resultsSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    function addOptionRow(quality, format, meta, type = 'normal') {
        const row = document.createElement('div');
        row.className = 'option-item';
        
        row.innerHTML = `
            <div class="option-info">
                <span class="option-quality">${quality}</span>
                <span class="option-meta">${format} • ${meta}</span>
            </div>
            <button class="download-btn" data-quality="${quality}">Download</button>
        `;
        
        const btn = row.querySelector('.download-btn');
        btn.addEventListener('click', (e) => startDownload(quality, e.target));
        
        optionsList.appendChild(row);
    }

    function startDownload(quality, btn) {
        const originalText = btn.textContent;
        btn.textContent = 'Starting...';
        document.querySelectorAll('.download-btn').forEach(b => b.disabled = true);

        const downloadUrl = `/api/download?url=${encodeURIComponent(currentUrl)}&quality=${encodeURIComponent(quality)}`;
        const link = document.createElement('a');
        link.href = downloadUrl;
        link.style.display = 'none';
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);

        setTimeout(() => {
            if (btn) btn.textContent = originalText;
            document.querySelectorAll('.download-btn').forEach(b => b.disabled = false);
        }, 2000);
    }
});
