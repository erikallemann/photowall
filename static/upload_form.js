const form = document.getElementById('uploadForm');
const fileInput = document.getElementById('file');
const captionInput = document.getElementById('caption');
const pinInput = document.getElementById('pin');
const submit = document.getElementById('uploadSubmit');
const statusEl = document.getElementById('status');
const issues = document.getElementById('uploadIssues');
const fields = [fileInput, captionInput, pinInput, submit];

let uploading = false;
let retryFiles = [];

function setStatus(message, kind = '') {
  statusEl.textContent = message;
  statusEl.className = kind;
}

function showIssue(message, file) {
  const issue = document.createElement('li');
  issue.textContent = file ? file.name + ' – ' + message : message;
  issues.append(issue);
  issues.hidden = false;
}

function clearIssues() {
  issues.replaceChildren();
  issues.hidden = true;
}

function setSubmitLabel() {
  submit.textContent = text(retryFiles.length ? 'Retry remaining photos' : 'Upload photos');
}

function setUploading(active) {
  uploading = active;
  for (const field of fields) field.disabled = active;
}

async function uploadPhoto(file, caption, headers) {
  const data = new FormData();
  data.append('file', file);
  if (caption) data.append('caption', caption);
  try {
    const response = await fetch('/upload', { method: 'POST', body: data, headers });
    return response.status;
  } catch {
    return null;
  }
}

function uploadIssue(status) {
  if (status === 400) return text('Unsupported or damaged photo.');
  if (status === 413) return text('Photo exceeds the upload size, pixel, or frame limit.');
  if (status === 429) return text('Too many uploads. Try again shortly.');
  if (status >= 400 && status < 500 && status !== 408) {
    return text('Upload failed ({status}).', { status });
  }
  return text('Upload could not be confirmed. Check the photo wall before trying again.');
}

fileInput.addEventListener('change', () => {
  retryFiles = [];
  setSubmitLabel();
  setStatus('');
  clearIssues();
});

form.addEventListener('submit', async event => {
  event.preventDefault();
  if (uploading) return;
  const files = retryFiles.length ? retryFiles : Array.from(fileInput.files);
  if (!files.length) {
    setStatus(text('Select photos first.'), 'error');
    return;
  }

  const caption = captionInput.value.trim();
  const pin = pinInput.value.trim();
  const headers = pin ? { 'X-Upload-Pin': pin } : {};
  const total = files.length;
  let uploaded = 0;
  retryFiles = [];
  clearIssues();
  setUploading(true);
  try {
    for (const [index, file] of files.entries()) {
      setStatus(total === 1 ? text('Uploading...')
        : text('Uploading {current} of {total}…', { current: index + 1, total }));
      const status = await uploadPhoto(file, caption, headers);
      if (status === 201) {
        uploaded++;
      } else if (status === 403) {
        showIssue(text('Incorrect PIN or uploads disabled.'));
        retryFiles.push(...files.slice(index));
        break;
      } else {
        showIssue(uploadIssue(status), file);
        if (status === 429) retryFiles.push(file);
      }
    }

    if (uploaded === total) {
      form.reset();
      setStatus(total === 1 ? text('Upload successful!') : text('Uploaded {count} photos.', { count: uploaded }), 'ok');
    } else {
      if (!retryFiles.length) fileInput.value = '';
      const message = total === 1 ? 'Uploaded {uploaded} of {total} photo.' : 'Uploaded {uploaded} of {total} photos.';
      setStatus(text(message, { uploaded, total }), 'error');
    }
    setSubmitLabel();
  } finally {
    setUploading(false);
  }
});
