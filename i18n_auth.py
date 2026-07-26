from __future__ import annotations

import html


def auth_page(mode: str, error: str = "") -> str:
    setup = mode == "setup"
    action = "/admin/setup" if setup else "/admin/login"
    error_html = f'<div class="error">{html.escape(error)}</div>' if error else ""
    confirm = (
        '<label><span id="confirmLabel">비밀번호 확인</span>'
        '<input type="password" name="confirm" minlength="8" required '
        'autocomplete="new-password"></label>'
        if setup
        else ""
    )
    mode_json = "true" if setup else "false"
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Project Neunggureongi</title><style>
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;padding:28px;background:radial-gradient(circle at 18% 15%,#254b48 0,#14253a 32%,#0b1427 72%);font-family:Inter,"Noto Sans KR",sans-serif;color:#172033}}
.shell{{position:relative;width:min(920px,100%);display:grid;grid-template-columns:1.12fr .88fr;background:white;border-radius:18px;overflow:hidden;box-shadow:0 30px 90px #0008}}
.language{{position:absolute;right:16px;top:14px;z-index:5;display:flex;gap:4px}}.language button{{width:auto;margin:0;padding:6px 9px;border:1px solid #d5dbe5;background:#fff;color:#475467;border-radius:6px;font-size:11px;cursor:pointer}}.language button.active{{background:#28664d;color:#fff;border-color:#28664d}}
.story{{position:relative;padding:48px;color:#edf8ed;background:linear-gradient(145deg,#173b36,#28664d)}}.story:after{{content:"S";position:absolute;right:18px;bottom:-58px;font-family:Georgia,serif;font-size:250px;color:#ffffff0b;transform:rotate(-18deg)}}
.version{{display:inline-block;padding:6px 10px;border:1px solid #d8efdc55;border-radius:20px;font-size:11px;font-weight:800;letter-spacing:.12em}}.brand{{margin:22px 0 5px;font-size:30px;letter-spacing:-.04em}}.english{{font-size:11px;letter-spacing:.18em;color:#b9ddc4}}
.legend{{margin-top:44px;position:relative;z-index:1}}.legend h2{{font-family:Georgia,"Noto Serif KR",serif;font-size:18px;margin:0 0 12px;color:#f4d98c}}.legend p{{color:#d1e5d6;font-size:13px;line-height:1.85;margin:0 0 16px}}.wish{{padding-left:14px;border-left:2px solid #e6c76b;color:#fff!important;font-weight:650}}
.box{{padding:48px 38px;align-self:center}}.box h1{{margin:0 0 8px;font-size:24px}}.box>p{{margin:0 0 24px;color:#667085;font-size:13px;line-height:1.6}}label{{display:block;font-size:12px;font-weight:700;margin-top:15px}}input{{width:100%;padding:12px;margin-top:7px;border:1px solid #cbd3df;border-radius:8px}}.submit{{width:100%;margin-top:24px;padding:12px;border:0;border-radius:8px;background:#28664d;color:white;font-weight:800;cursor:pointer}}.submit:hover{{background:#1e523d}}.error{{background:#fff0f1;color:#b4232d;padding:10px;border-radius:7px;font-size:13px}}
@media(max-width:720px){{body{{padding:14px}}.shell{{grid-template-columns:1fr}}.story{{padding:30px}}.legend{{margin-top:26px}}.box{{padding:42px 28px 32px}}}}
</style></head><body><main class="shell">
<div class="language"><button type="button" id="koButton" onclick="setLanguage('ko')">KO</button><button type="button" id="enButton" onclick="setLanguage('en')">EN</button></div>
<section class="story"><span class="version">VERSION 1.0.0</span><h1 class="brand" id="brand">능구렁이</h1><div class="english">PROJECT NEUNGGUREONGI</div>
<div class="legend"><h2 id="legendTitle">오래된 터를 지키는 존재</h2><p id="legendText">우리 옛이야기에서 큰 구렁이는 집과 터에 오래 머물며 복을 지키는 신령한 존재로 여겨지곤 했습니다. 조용히 주변을 살피고 땅의 굽이를 타고 흐르는 모습에는 삶의 터전을 아끼는 마음이 담겨 있습니다.</p><p class="wish" id="wishText">그 이름처럼 지형과 도면, 공간 데이터의 굽이굽이를 능숙하게 잇고, 모든 공간정보를 빠르고 든든하게 서비스하고자 하는 염원을 이 프로젝트에 담았습니다.</p></div></section>
<form class="box" action="{action}" method="post"><h1 id="formTitle"></h1><p id="formNote"></p>{error_html}
<label><span id="idLabel">ID</span><input name="username" maxlength="100" required autofocus autocomplete="username"></label>
<label><span id="passwordLabel">비밀번호</span><input type="password" name="password" minlength="8" required autocomplete="{'new-password' if setup else 'current-password'}"></label>
{confirm}<button class="submit" id="submitButton"></button></form></main>
<script>
const isSetup={mode_json};
const messages={{
 ko:{{brand:'능구렁이',legendTitle:'오래된 터를 지키는 존재',legendText:'우리 옛이야기에서 큰 구렁이는 집과 터에 오래 머물며 복을 지키는 신령한 존재로 여겨지곤 했습니다. 조용히 주변을 살피고 땅의 굽이를 타고 흐르는 모습에는 삶의 터전을 아끼는 마음이 담겨 있습니다.',wishText:'그 이름처럼 지형과 도면, 공간 데이터의 굽이굽이를 능숙하게 잇고, 모든 공간정보를 빠르고 든든하게 서비스하고자 하는 염원을 이 프로젝트에 담았습니다.',loginTitle:'관리자 로그인',setupTitle:'관리자 계정 만들기',loginNote:'레이어 관리를 계속하려면 로그인하세요.',setupNote:'최초 1회만 설정합니다. ID와 비밀번호는 암호화된 바이너리로 저장됩니다.',id:'ID',password:'비밀번호',confirm:'비밀번호 확인',login:'로그인',setup:'암호화 계정 생성'}},
 en:{{brand:'Neunggureongi',legendTitle:'Guardian of the Land',legendText:'In Korean folklore, a great serpent was sometimes regarded as a spiritual guardian that remained near a home and protected its fortune. Its quiet path along the curves of the land reflects care for the places where people live.',wishText:'Inspired by its name, this project aspires to connect terrain, drawings, and spatial data with skill—and to serve every kind of geospatial information quickly and reliably.',loginTitle:'Administrator Login',setupTitle:'Create Administrator Account',loginNote:'Sign in to continue managing layers.',setupNote:'This one-time setup stores the ID and password in encrypted binary storage.',id:'ID',password:'Password',confirm:'Confirm password',login:'Sign in',setup:'Create encrypted account'}}
}};
function setLanguage(lang){{const m=messages[lang]||messages.ko;document.documentElement.lang=lang;document.title=(isSetup?m.setupTitle:m.loginTitle)+' · Project Neunggureongi';for(const id of ['brand','legendTitle','legendText','wishText'])document.getElementById(id).textContent=m[id];document.getElementById('formTitle').textContent=isSetup?m.setupTitle:m.loginTitle;document.getElementById('formNote').textContent=isSetup?m.setupNote:m.loginNote;document.getElementById('idLabel').textContent=m.id;document.getElementById('passwordLabel').textContent=m.password;if(document.getElementById('confirmLabel'))document.getElementById('confirmLabel').textContent=m.confirm;document.getElementById('submitButton').textContent=isSetup?m.setup:m.login;document.getElementById('koButton').classList.toggle('active',lang==='ko');document.getElementById('enButton').classList.toggle('active',lang==='en');localStorage.setItem('neunggureongi_language',lang)}}
setLanguage(localStorage.getItem('neunggureongi_language')||'ko');
</script></body></html>"""
