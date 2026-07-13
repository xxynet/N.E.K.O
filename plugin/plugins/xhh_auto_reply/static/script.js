const pluginId = 'xhh_auto_reply';
const RUNS_URL = '/runs';
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

async function callPlugin(entry, args = {}) {
  const response = await fetch(RUNS_URL, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({plugin_id: pluginId, entry_id: entry, args})
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const created = await response.json();
  const runId = created.run_id || created.id;
  if (!runId) throw new Error('未获得 run_id');
  const deadline = Date.now() + 70000;
  while (Date.now() < deadline) {
    const poll = await fetch(`${RUNS_URL}/${runId}`);
    if (poll.ok) {
      const record = await poll.json();
      if (record.status === 'succeeded') {
        const exported = await fetch(`${RUNS_URL}/${runId}/export`);
        const payload = exported.ok ? await exported.json() : {};
        const item = (payload.items || []).find(value => value.type === 'json' && value.json) || (payload.items || [])[0];
        let result = item?.json || {};
        while (result?.data && typeof result.data === 'object' && ('success' in result.data || 'error' in result.data)) result = result.data;
        if (result.error) throw new Error(result.error.message || String(result.error));
        return result.value || result.data || result;
      }
      if (['failed','canceled','timeout'].includes(record.status)) throw new Error(record.error?.message || record.message || record.status);
    }
    await sleep(400);
  }
  throw new Error('调用超时');
}

const $ = id => document.getElementById(id);
function toast(message) { const el=$('toast'); el.textContent=message; el.classList.add('show'); clearTimeout(toast.timer); toast.timer=setTimeout(()=>el.classList.remove('show'),3500); }
function ids(value) { return String(value||'').split(/[,，\s]+/).filter(Boolean).map(Number).filter(Number.isFinite); }
function updatePublishState() {
  const validLink=Number($('linkId').value)>0, hasText=!!$('commentText').value.trim();
  const ready=validLink&&hasText;
  $('send').disabled=!ready;
  $('publishState').textContent=ready ? '已就绪：点击“真实发布”将立即发送，不再弹出二次确认。' : '填写有效的帖子 Link ID 和评论正文后可发布。';
}

function applyState(data) {
  const configured = !!data.configured;
  $('status').textContent = configured ? (data.auto_reply_running ? '监听中' : '已配置') : '未配置';
  $('status').classList.toggle('ok', configured);
  const settings = data.settings || {};
  $('heyboxId').value = data.heybox_id || '';
  $('dryRun').checked = settings.dry_run !== false;
  $('pollInterval').value = settings.poll_interval_seconds || 60;
  $('requestInterval').value = settings.min_request_interval_seconds || 2;
  $('allowedUsers').value = (settings.allowed_user_ids || []).join(',');
  $('replyPrompt').value = settings.reply_prompt || '';
  const events = data.recent_events || [];
  $('events').textContent = events.length ? events.slice().reverse().map(item => JSON.stringify(item, null, 2)).join('\n\n') : '暂无记录';
}

async function refresh() { try { applyState(await callPlugin('get_dashboard_state')); } catch (error) { toast(error.message); } }
async function act(button, entry, args={}) { button.disabled=true; try { const data=await callPlugin(entry,args); if (data?.settings || 'configured' in (data||{})) applyState(data); toast('操作成功'); return data; } catch(error) { toast(error.message); throw error; } finally { button.disabled=false; } }

$('importCookie').onclick = async event => { await act(event.currentTarget,'import_cookie',{cookie:$('cookie').value,heybox_id:$('heyboxId').value}); $('cookie').value=''; };
$('clearCookie').onclick = event => act(event.currentTarget,'clear_cookie');
$('saveConfig').onclick = event => act(event.currentTarget,'update_config',{
  dry_run:$('dryRun').checked,poll_interval_seconds:Number($('pollInterval').value),
  min_request_interval_seconds:Number($('requestInterval').value),allowed_user_ids:ids($('allowedUsers').value),reply_prompt:$('replyPrompt').value
});
$('startAuto').onclick = event => act(event.currentTarget,'start_auto_reply');
$('stopAuto').onclick = event => act(event.currentTarget,'stop_auto_reply');
$('pollOnce').onclick = async event => { const data=await act(event.currentTarget,'run_poll_once'); toast(`检查 ${data.checked||0} 条，处理 ${data.handled||0} 条`); await refresh(); };
$('generate').onclick = async event => {
  const linkId=Number($('linkId').value); if(!linkId){toast('请填写帖子 Link ID');return;}
  const data=await act(event.currentTarget,'generate_post_comment',{link_id:linkId,request:$('aiRequest').value||'结合帖子内容生成自然评论',publish:false});
  $('commentText').value=data.text||'';
  updatePublishState();
};
$('linkId').addEventListener('input',updatePublishState);
$('commentText').addEventListener('input',updatePublishState);
$('send').onclick = async event => {
  const linkId=Number($('linkId').value), commentId=Number($('commentId').value), rootId=Number($('rootId').value), text=$('commentText').value.trim();
  if(!linkId||!text){toast('请填写帖子 Link ID 和评论正文');return;}
  toast('正在发布到小黑盒…');
  if(commentId) await act(event.currentTarget,'reply_comment',{link_id:linkId,comment_id:commentId,root_id:rootId||commentId,text});
  else await act(event.currentTarget,'publish_post_comment',{link_id:linkId,text});
  updatePublishState();
  await refresh();
};

refresh();
updatePublishState();
