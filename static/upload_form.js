const form = document.getElementById('uploadForm');
const fileInput = document.getElementById('file');
const captionInput = document.getElementById('caption');
const pinInput = document.getElementById('pin');
const statusEl = document.getElementById('status');

function setStatus(text, cls){
  statusEl.textContent = text;
  statusEl.className = cls || '';
}

form.addEventListener('submit', async (ev)=>{
  ev.preventDefault();
  const file = fileInput.files[0];
  if(!file){
    setStatus(text('Select a photo first.'), 'error');
    return;
  }
  const fd = new FormData();
  fd.append('file', file);
  const caption = captionInput.value.trim();
  if(caption) fd.append('caption', caption);

  const headers = {};
  const pin = pinInput.value.trim();
  if(pin) headers['X-Upload-Pin'] = pin;

  setStatus(text('Uploading...'), '');
  try{
    const res = await fetch('/upload', {method:'POST', body: fd, headers});
    if(res.status === 201){
      setStatus(text('Upload successful!'), 'ok');
      form.reset();
    } else if(res.status === 403){
      setStatus(text('Incorrect PIN or uploads disabled.'), 'error');
    } else if(res.status === 413){
      setStatus(text('Photo exceeds the upload size, pixel, or frame limit.'), 'error');
    } else {
      setStatus(text('Upload failed ({status}).', {status: res.status}), 'error');
    }
  } catch(err){
    setStatus(text('Network error, please try again.'), 'error');
  }
});
