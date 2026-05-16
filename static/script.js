// Elementet e DOM-it
const urlInput = document.getElementById("url-input");
const infoBtn = document.getElementById("info-btn");
const downloadBtn = document.getElementById("download-btn");
const videoInfo = document.getElementById("video-info");
const qualitySection = document.getElementById("quality-section");
const status = document.getElementById("status");
const loader = document.getElementById("loader");
const loaderText = document.getElementById("loader-text");

// Funksione ndihmëse
function showLoader(text) {
    loaderText.textContent = text;
    loader.classList.remove("hidden");
}

function hideLoader() {
    loader.classList.add("hidden");
}

function showStatus(message, type) {
    status.textContent = message;
    status.className = `status ${type}`;
    status.classList.remove("hidden");
}

function hideStatus() {
    status.classList.add("hidden");
}

function formatDuration(seconds) {
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${mins}:${secs.toString().padStart(2, "0")}`;
}

// Merr informacion për videon
async function getInfo() {
    const url = urlInput.value.trim();
    
    if (!url) {
        showStatus("Të lutem ngjit një URL të YouTube", "error");
        return;
    }
    
    hideStatus();
    videoInfo.classList.add("hidden");
    qualitySection.classList.add("hidden");
    showLoader("Duke marrë informacion...");
    infoBtn.disabled = true;
    
    try {
        const response = await fetch("/api/info", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url })
        });
        
        const data = await response.json();
        
        if (!response.ok) {
            throw new Error(data.error || "Gabim i panjohur");
        }
        
        // Shfaq informacionin
        document.getElementById("thumbnail").src = data.thumbnail;
        document.getElementById("video-title").textContent = data.title;
        document.getElementById("video-uploader").textContent = `📺 ${data.uploader}`;
        document.getElementById("video-duration").textContent = `⏱ ${formatDuration(data.duration)}`;
        
        videoInfo.classList.remove("hidden");
        qualitySection.classList.remove("hidden");
    } catch (error) {
        showStatus(`❌ ${error.message}`, "error");
    } finally {
        hideLoader();
        infoBtn.disabled = false;
    }
}

// Shkarko audion
async function downloadAudio() {
    const url = urlInput.value.trim();
    const quality = document.getElementById("quality-select").value;
    
    if (!url) {
        showStatus("URL-ja mungon", "error");
        return;
    }
    
    hideStatus();
    showLoader("Duke shkarkuar dhe konvertuar në MP3... (mund të zgjasë pak)");
    downloadBtn.disabled = true;
    
    try {
        const response = await fetch("/api/download", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url, quality })
        });
        
        const data = await response.json();
        
        if (!response.ok) {
            throw new Error(data.error || "Gabim gjatë shkarkimit");
        }
        
        // Krijo një link për shkarkim dhe kliko-e automatikisht
        const downloadUrl = `/api/file/${data.download_id}?name=${encodeURIComponent(data.filename)}`;
        const link = document.createElement("a");
        link.href = downloadUrl;
        link.download = data.filename;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        
        showStatus(`✅ U shkarkua: ${data.filename}`, "success");
    } catch (error) {
        showStatus(`❌ ${error.message}`, "error");
    } finally {
        hideLoader();
        downloadBtn.disabled = false;
    }
}

// Mbështet Enter në input
urlInput.addEventListener("keypress", (e) => {
    if (e.key === "Enter") getInfo();
});
