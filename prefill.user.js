// ==UserScript==
// @name         Cupertino 网球场 · 预填助手
// @namespace    tennis-booker
// @version      1.0
// @description  订场页自动预填 Event name 和 Custom Questions 弹窗(I agree + 90分钟下拉)，只留"人工验证题"给你本人答。绝不自动答验证题、绝不自动 Save/下单。
// @match        https://anc.apm.activecommunities.com/cupertino/reservation/*
// @run-at       document-idle
// @grant        none
// ==/UserScript==
(function () {
  'use strict';

  const EVENT_NAME = 'Tennis';        // 自动填入的 Event name，可改
  const PLAY_TYPE = 'Singles';        // 想默认 Doubles 就改这里；留空=不动(用站点默认 Singles)

  // ---------- 小提示条 ----------
  function toast(msg, color) {
    let t = document.getElementById('__tennis_toast');
    if (!t) {
      t = document.createElement('div');
      t.id = '__tennis_toast';
      t.style.cssText =
        'position:fixed;z-index:999999;top:16px;right:16px;background:#0a7d36;color:#fff;' +
        'padding:11px 15px;border-radius:8px;font:14px/1.45 -apple-system,sans-serif;' +
        'box-shadow:0 3px 12px rgba(0,0,0,.35);max-width:340px;cursor:default';
      document.body.appendChild(t);
    }
    t.style.background = color || '#0a7d36';
    t.textContent = msg;
    t.style.display = 'block';
    clearTimeout(t.__h);
    t.__h = setTimeout(() => { t.style.display = 'none'; }, 7000);
  }

  function setNativeValue(input, value) {
    const proto = Object.getPrototypeOf(input);
    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
    if (desc && desc.set) desc.set.call(input, value); else input.value = value;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
  }

  function textOf(el) { return (el && el.innerText || '').trim(); }

  // ---------- 1) Event name 自动填 ----------
  function fillEventName() {
    let input = null;
    const lbl = [...document.querySelectorAll('label, span, div')]
      .find(e => e.children.length === 0 && /^\s*Event name/i.test(e.textContent || ''));
    if (lbl) {
      let scope = lbl;
      for (let i = 0; i < 4 && scope && !input; i++) { input = scope.querySelector('input[type=text]'); scope = scope.parentElement; }
    }
    if (!input) input = [...document.querySelectorAll('input[type=text]')].find(i => i.offsetParent);
    if (input && !input.value) { setNativeValue(input, EVENT_NAME); }
  }

  // ---------- 2) Custom Questions 弹窗自动填 ----------
  function fillModal(modal) {
    const done = [];

    // (a) 播放类型（站点默认已选 Singles；按需确保选中 PLAY_TYPE）
    if (PLAY_TYPE) {
      const r = [...modal.querySelectorAll('input[type=radio]')]
        .find(x => new RegExp('^' + PLAY_TYPE + '$', 'i').test(textOf(x.closest('label') || x.parentElement)));
      if (r && !r.checked) { r.click(); }
    }

    // (b) "I agree" radio（防 bot 条款）
    const agree = [...modal.querySelectorAll('input[type=radio]')]
      .find(x => /^I agree$/i.test(textOf(x.closest('label') || x.parentElement)));
    if (agree && !agree.checked) { agree.click(); done.push('I agree'); }

    // (c) 90 分钟下拉 → 选 "I agree"
    const cont = [...modal.querySelectorAll('.question-answer-container, .question-answer, .afx-col')]
      .find(q => /90\s*minutes/i.test(q.innerText || ''));
    const dd = cont && cont.querySelector('.dropdown, [class*="dropdown"]');
    if (dd && /select one/i.test(dd.innerText || '')) {
      const btn = dd.querySelector('.dropdown__button, [class*="dropdown__button"]') || dd;
      btn.click();
      setTimeout(() => {
        const opt = [...document.querySelectorAll(
          '.dropdown__menu__option, [class*="dropdown__menu"] [class*="option"], .dropdown li')]
          .find(o => /^\s*I agree\s*$/i.test(o.innerText || ''));
        if (opt) opt.click();
      }, 250);
      done.push('90分钟');
    }

    // (d) 聚焦"人工验证题"输入框（留给用户答；绝不自动填）
    const vq = [...modal.querySelectorAll('input[type=text], textarea')].find(i => i.offsetParent);
    if (vq) setTimeout(() => { try { vq.focus(); } catch (e) {} }, 500);

    if (done.length) {
      toast('✅ 已预填：' + done.join(' + ') + '。\n请自己答验证题（如 first letter of "Friday" → F），再点 Save。');
    }
  }

  // ---------- 监听弹窗 / 选位 ----------
  let lastModalFill = 0;
  function tick() {
    const modal = [...document.querySelectorAll('[role=dialog], [class*="modal"], [class*="dialog"]')]
      .find(m => /Custom questions/i.test(m.innerText || '') && m.querySelector('input[type=radio]'));
    if (modal && Date.now() - lastModalFill > 1500) { lastModalFill = Date.now(); fillModal(modal); }
    if (/Booking\(s\)\s*selected/i.test(document.body.innerText || '')) fillEventName();
  }

  const obs = new MutationObserver(() => tick());
  obs.observe(document.body, { childList: true, subtree: true });
  setTimeout(tick, 1200);
  console.log('[网球预填助手] 已加载 v1');
})();
