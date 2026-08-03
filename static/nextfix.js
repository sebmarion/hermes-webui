/* User-confirmed Safe Change report capture. No automatic generation or release action. */
(function(){
  'use strict';

  function textForMessage(message){
    if(!message) return '';
    if(typeof msgContent==='function') return String(msgContent(message)||'').trim();
    if(typeof message.content==='string') return message.content.trim();
    if(Array.isArray(message.content)) return message.content.map(function(part){
      return part&&typeof part.text==='string'?part.text:'';
    }).join('\n').trim();
    return '';
  }

  function reportTextForMessage(rawIdx){
    var messages=(typeof S!=='undefined'&&Array.isArray(S.messages))?S.messages:[];
    var message=messages[Number(rawIdx)];
    var assistant=textForMessage(message).slice(0,2800);
    var user='';
    for(var i=Number(rawIdx)-1;i>=0;i--){
      if(messages[i]&&messages[i].role==='user'){
        user=textForMessage(messages[i]).slice(0,900);
        break;
      }
    }
    var sections=['The assistant response did not meet my expectation.'];
    if(user) sections.push('User request:\n'+user);
    if(assistant) sections.push('Assistant response:\n'+assistant);
    return sections.join('\n\n').slice(0,4096);
  }

  function setStatus(text, kind){
    var status=document.getElementById('nextfixStatus');
    if(!status) return;
    status.textContent=text||'';
    status.dataset.kind=kind||'';
    status.hidden=!text;
  }

  window.openNextfixReport=function(rawIdx){
    var dialog=document.getElementById('nextfixDialog');
    var observed=document.getElementById('nextfixObserved');
    var expected=document.getElementById('nextfixExpected');
    if(!dialog||!observed||!expected) return;
    var sid=(typeof S!=='undefined'&&S.session&&S.session.session_id)||'';
    dialog.dataset.messageIndex=String(rawIdx);
    dialog.dataset.sessionId=String(sid);
    observed.value=reportTextForMessage(rawIdx);
    expected.value='';
    setStatus('', '');
    if(typeof dialog.showModal==='function') dialog.showModal();
    else dialog.setAttribute('open','open');
    expected.focus();
  };

  function closeDialog(){
    var dialog=document.getElementById('nextfixDialog');
    if(!dialog) return;
    if(typeof dialog.close==='function') dialog.close();
    else dialog.removeAttribute('open');
  }

  document.addEventListener('DOMContentLoaded',function(){
    var dialog=document.getElementById('nextfixDialog');
    var form=document.getElementById('nextfixForm');
    var cancel=document.getElementById('nextfixCancel');
    var cancelFooter=document.getElementById('nextfixCancelFooter');
    if(!dialog||!form) return;
    if(cancel) cancel.addEventListener('click',closeDialog);
    if(cancelFooter) cancelFooter.addEventListener('click',closeDialog);
    dialog.addEventListener('click',function(event){
      if(event.target===dialog) closeDialog();
    });
    form.addEventListener('submit',async function(event){
      event.preventDefault();
      var observed=document.getElementById('nextfixObserved');
      var expected=document.getElementById('nextfixExpected');
      var sid=dialog.dataset.sessionId||'';
      var rawIdx=Number(dialog.dataset.messageIndex);
      var expectedText=String(expected&&expected.value||'').trim();
      if(!expectedText){
        setStatus((typeof t==='function'?t('nextfix_expected_required'):'Describe what should have happened before capturing this report.'), 'error');
        expected&&expected.focus();
        return;
      }
      var submit=form.querySelector('button[type="submit"]');
      if(submit) submit.disabled=true;
      setStatus((typeof t==='function'?t('nextfix_capturing'):'Capturing locally…'), 'working');
      try{
        var result=await api('/api/nextfix',{
          method:'POST',
          body:JSON.stringify({
            observed:String(observed&&observed.value||'').slice(0,4096),
            expected:expectedText.slice(0,4096),
            session_id:sid,
            message_index:Number.isFinite(rawIdx)?rawIdx:null
          })
        });
        closeDialog();
        if(typeof showToast==='function') showToast(
          (typeof t==='function'?t('nextfix_captured'):'Captured for the next release: ')+((result&&result.issue_id)||''),
          4200,
          'success'
        );
      }catch(error){
        setStatus(error&&error.message||((typeof t==='function'?t('nextfix_capture_error'):'Could not capture this report.')), 'error');
      }finally{
        if(submit) submit.disabled=false;
      }
    });
  });
})();
