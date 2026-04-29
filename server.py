import eventlet
eventlet.monkey_patch()

import random, time, sqlite3, os, string
from flask import Flask, Response
from flask_socketio import SocketIO, emit, join_room

app = Flask(__name__)
app.config['SECRET_KEY'] = 'lite-games-final'
socketio = SocketIO(app, async_mode='eventlet')

conn = sqlite3.connect("keno_app.db", check_same_thread=False)
conn.execute("""CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, balance REAL DEFAULT 100.0)""")
conn.execute("""CREATE TABLE IF NOT EXISTS rounds (id INTEGER PRIMARY KEY, drawn_numbers TEXT, timestamp REAL)""")
conn.execute("""CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, round_id INTEGER,
    numbers TEXT, amount REAL, ticket_mask TEXT, win_amount REAL DEFAULT 0
)""")
conn.commit()

PAYTABLE = {
    1:{0:0,1:2},2:{0:0,1:0,2:4},3:{0:0,1:0,2:2,3:12},
    4:{0:0,1:0,2:1,3:5,4:30},5:{0:0,1:0,2:0,3:4,4:15,5:80},
    6:{0:0,1:0,2:0,3:3,4:10,5:50,6:200},7:{0:0,1:0,2:0,3:2,4:5,5:20,6:100,7:500},
    8:{0:0,1:0,2:0,3:1,4:3,5:10,6:40,7:200,8:1000},
    9:{0:0,1:0,2:0,3:1,4:2,5:8,6:30,7:150,8:600,9:2000},
    10:{0:0,1:0,2:0,3:1,4:2,5:5,6:25,7:100,8:500,9:1000,10:5000}
}

class Round:
    def __init__(self, rid):
        self.id = rid
        self.start_time = time.time()
        self.bets = {}
        self.drawn = None
        self.timer = 60

current_round = Round(1)

def gen_mask():
    r = ''.join(random.choices(string.digits, k=4))
    return r[0] + '***' + r[3]

def process_draw(round_obj):
    drawn = sorted(random.sample(range(1,81),20))
    round_obj.drawn = drawn
    conn.execute("INSERT INTO rounds (id, drawn_numbers, timestamp) VALUES (?,?,?)",
                 (round_obj.id, ','.join(map(str,drawn)), time.time()))
    conn.commit()
    winners = {}
    for uid, tickets in round_obj.bets.items():
        total_win = 0
        for t in tickets:
            matches = len(set(t['numbers']) & set(drawn))
            spots = len(t['numbers'])
            mult = PAYTABLE.get(spots,{}).get(matches,0)
            win = t['amount'] * mult
            total_win += win
            conn.execute("INSERT INTO tickets (user_id,round_id,numbers,amount,ticket_mask,win_amount) VALUES (?,?,?,?,?,?)",
                         (uid, round_obj.id, ','.join(map(str,t['numbers'])), t['amount'], t['mask'], win))
            conn.commit()
        cur = conn.cursor()
        cur.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (total_win, uid))
        conn.commit()
        winners[uid] = total_win
    return drawn, winners

@socketio.on('connect')
def on_connect():
    join_room('round')
    elapsed = time.time() - current_round.start_time
    emit('round_state', {'round_id':current_round.id, 'remaining':max(0,current_round.timer-elapsed), 'drawn':current_round.drawn})

def get_balance(uid):
    cur = conn.cursor()
    cur.execute("SELECT balance FROM users WHERE user_id=?", (uid,))
    row = cur.fetchone()
    if row is None:
        cur.execute("INSERT INTO users (user_id) VALUES (?)", (uid,))
        conn.commit()
        return 100.0
    return row[0]

@socketio.on('request_balance')
def bal(data): emit('balance', {'balance':get_balance(data['user_id'])})

@socketio.on('deposit')
def deposit(data):
    uid, amt = data['user_id'], float(data['amount'])
    if amt <= 0: return
    conn.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (amt, uid))
    conn.commit()
    emit('balance', {'balance':get_balance(uid)})

@socketio.on('place_bet')
def bet(data):
    uid = data['user_id']
    nums = sorted(map(int, data['numbers']))
    amt = float(data['amount'])
    spots = len(nums)
    if spots<1 or spots>10: emit('error',{'message':'Pick 1-10 numbers'}); return
    if amt > get_balance(uid): emit('error',{'message':'Insufficient balance'}); return
    if len(current_round.bets.get(uid,[])) >= 5: emit('error',{'message':'Max 5 tickets'}); return
    conn.execute("UPDATE users SET balance = balance - ? WHERE user_id=?", (amt, uid))
    conn.commit()
    mask = gen_mask()
    ticket = {'numbers':nums, 'amount':amt, 'mask':mask}
    current_round.bets.setdefault(uid,[]).append(ticket)
    tickets = current_round.bets[uid]
    emit('bet_success', {'tickets':[{'mask':t['mask'],'numbers':t['numbers'],'amount':t['amount']} for t in tickets], 'balance':get_balance(uid)})

@socketio.on('request_tickets')
def req_tickets(data):
    uid = data['user_id']
    tickets = current_round.bets.get(uid,[])
    emit('your_tickets', {'tickets':[{'mask':t['mask'],'numbers':t['numbers'],'amount':t['amount']} for t in tickets]})

@socketio.on('request_history')
def hist(data):
    uid = data['user_id']
    rows = conn.execute("""SELECT t.round_id, t.numbers, t.amount, t.ticket_mask, t.win_amount, r.drawn_numbers
                          FROM tickets t JOIN rounds r ON t.round_id=r.id
                          WHERE t.user_id=? ORDER BY t.round_id DESC LIMIT 50""", (uid,)).fetchall()
    out = [{'round_id':r[0],'numbers':list(map(int,r[1].split(','))),'amount':r[2],'mask':r[3],'win':r[4],
            'drawn':list(map(int,r[5].split(','))) if r[5] else []} for r in rows]
    emit('history_data', {'history':out})

@socketio.on('request_leaderboard')
def leaderboard():
    rows = conn.execute("SELECT ticket_mask, amount, win_amount FROM tickets WHERE win_amount>0 ORDER BY win_amount DESC LIMIT 10").fetchall()
    emit('leaderboard_data', {'leaders':[{'mask':r[0],'bet':r[1],'win':r[2]} for r in rows]})

@socketio.on('request_stats')
def stats():
    rows = conn.execute("SELECT drawn_numbers FROM rounds ORDER BY id DESC LIMIT 100").fetchall()
    freq = [0]*81
    for r in rows:
        for n in map(int, r[0].split(',')):
            if 1<=n<=80: freq[n] += 1
    emit('stats_data', {'freq': freq[1:]})

def round_loop():
    global current_round
    while True:
        socketio.sleep(current_round.timer)
        drawn, winners = process_draw(current_round)
        socketio.emit('draw_result', {'round_id':current_round.id, 'drawn':drawn, 'winners':winners}, room='round')
        current_round = Round(current_round.id+1)
        socketio.emit('new_round', {'round_id':current_round.id, 'duration':current_round.timer}, room='round')

HTML = r'''<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script src="https://cdn.socket.io/4.5.0/socket.io.min.js"></script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0b0f14; color: #d1d4d8; padding: 12px 8px; min-height: 100vh; user-select: none; -webkit-tap-highlight-color: transparent; }
.top-bar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px; }
.balance { background: #152028; padding: 6px 14px; border-radius: 20px; font-weight: 600; font-size: 14px; color: #fff; }
.deposit-btn { background: #2e7d32; color: white; border: none; padding: 6px 14px; border-radius: 20px; font-size: 14px; font-weight: 600; cursor: pointer; }
.round-id { font-size: 12px; color: #8899aa; margin-bottom: 2px; text-align: left; }
.timer { font-size: 32px; font-weight: 700; color: #ffaa00; margin: 2px 0 6px; text-align: center; }
.circles { display: flex; justify-content: space-between; margin: 0 0 6px; }
.circle { width: 32px; height: 32px; border-radius: 50%; background: #1c2636; display: flex; align-items: center; justify-content: center; font-weight: bold; font-size: 15px; color: #aaa; border: 2px solid #3a4a5a; }
.picker-info { font-size: 12px; color: #8899aa; margin: 0 0 2px; text-align: center; }
.grid { display: grid; grid-template-columns: repeat(10, 1fr); gap: 3px; margin: 6px 0; }
.num { background: #1c2636; border-radius: 4px; padding: 9px 0; font-size: 13px; font-weight: 500; text-align: center; cursor: pointer; color: #c0c8d0; transition: background 0.1s; }
.num.selected { background: #4caf50; color: white; font-weight: bold; }
.bet-controls { display: flex; align-items: center; justify-content: center; margin: 12px 0; gap: 6px; }
.bet-amount { font-size: 20px; font-weight: bold; min-width: 35px; text-align: center; color: white; }
.btn-sm { background: #2a3a4a; border: none; color: white; font-size: 20px; width: 32px; height: 32px; border-radius: 5px; cursor: pointer; line-height: 32px; text-align: center; }
.btn-x2, .btn-max { background: #e55300; color: white; border: none; padding: 8px 12px; border-radius: 5px; font-weight: bold; font-size: 14px; cursor: pointer; }
.place-bet-btn { background: #e55300; color: white; border: none; padding: 14px; border-radius: 7px; font-size: 18px; font-weight: bold; width: 100%; margin: 8px 0; cursor: pointer; text-transform: uppercase; letter-spacing: 1px; }
.ticket-area { margin: 6px 0; display: none; }
.ticket-label { font-size: 12px; color: #8899aa; margin-bottom: 4px; text-align: left; }
.ticket-list { background: #131c26; border-radius: 6px; padding: 6px 8px; }
.ticket-item { display: flex; justify-content: space-between; font-size: 12px; padding: 4px 0; border-bottom: 1px solid #233045; }
.ticket-item:last-child { border-bottom: none; }
.ticket-id { font-family: monospace; color: #ccc; width: 55px; }
.ticket-nums { font-family: monospace; color: #aaa; flex: 1; padding-left: 6px; }
.ticket-amount { min-width: 60px; text-align: right; }
.ticket-status { color: #d4a017; font-weight: 500; min-width: 55px; text-align: right; }
.tabs { display: flex; justify-content: space-around; background: #152028; padding: 10px 0; border-radius: 8px; margin-top: 10px; font-size: 13px; color: #667788; font-weight: 600; }
.tab { cursor: pointer; padding: 0 3px; }
.tab.active { color: #4caf50; }
#drawArea { display: none; }
.draw-id { font-size: 12px; color: #8899aa; margin-bottom: 12px; }
.draw-grid { margin: 16px 0; }
.draw-row { display: flex; justify-content: center; gap: 5px; margin-bottom: 5px; }
.draw-num { background: #1c2636; width: 30px; height: 34px; display: flex; align-items: center; justify-content: center; border-radius: 4px; font-size: 16px; font-weight: 600; color: transparent; transition: all 0.2s; }
.draw-num.show { background: #ed4452; color: white; }
.draw-extra-row { justify-content: center; gap: 14px; }
.draw-progress { font-size: 18px; margin: 10px 0; color: #ffaa00; text-align: center; }
.draw-result-btn { background: #2e7d32; color: white; border: none; padding: 12px; border-radius: 7px; font-size: 16px; font-weight: bold; width: 100%; cursor: pointer; margin-top: 16px; display: none; }
.back-btn { background: #2e7d32; color: white; border: none; padding: 12px; border-radius: 7px; font-size: 16px; font-weight: bold; width: 100%; cursor: pointer; margin-top: 12px; display: none; }
.fairness-footer { text-align: center; margin-top: 16px; padding-top: 12px; border-top: 1px solid #2a3a4a; font-size: 11px; color: #667788; }
.tab-content { display: none; }
.tab-content.active { display: block; }
.leader-table { width: 100%; border-collapse: collapse; margin-top: 8px; }
.leader-table th, .leader-table td { padding: 6px 3px; font-size: 13px; border-bottom: 1px solid #233045; text-align: left; color: #d1d4d8; }
.leader-table th { color: #8899aa; font-weight: 500; }
.stats-table { display: grid; grid-template-columns: repeat(10, 1fr); gap: 3px; margin: 8px 0; }
.stats-cell { background: #1c2636; border-radius: 3px; padding: 6px 0; text-align: center; font-size: 12px; color: #c0c8d0; }
.stats-cell .freq { font-size: 16px; font-weight: bold; color: #ffffff; }
.history-item { background: #131c26; border-radius: 6px; padding: 8px; margin-bottom: 6px; font-size: 13px; }
</style>
</head>
<body>
<div id="gameScreen">
  <div class="top-bar">
    <span class="balance" id="balanceDisplay">0.00 ETB</span>
    <button class="deposit-btn" onclick="deposit()">Deposit</button>
  </div>
  <div class="round-id" id="roundId">ID: 1</div>
  <div class="timer" id="timerDisplay">00:60</div>
  <div class="circles" id="circlesArea">
    <span class="circle">80</span>
    <span class="circle">70</span>
  </div>

  <div id="pickerArea">
    <div class="picker-info">Choose 10 numbers · From 1 to 80</div>
    <div class="grid" id="numberGrid"></div>
    <div class="bet-controls">
      <button class="btn-sm" onclick="adjustBet(-1)">-</button>
      <span class="bet-amount" id="betAmountDisplay">2</span>
      <button class="btn-sm" onclick="adjustBet(1)">+</button>
      <button class="btn-x2" onclick="setBet(betAmount*2)">X2</button>
      <button class="btn-max" onclick="setBet(balance)">MAX</button>
    </div>
    <button class="place-bet-btn" onclick="placeTicket()">BET</button>
    <div class="ticket-area" id="ticketArea">
      <div class="ticket-label">My Tickets</div>
      <div class="ticket-list" id="ticketList"></div>
    </div>
  </div>

  <div id="drawArea">
    <div class="draw-id" id="drawRoundId">ID: 1</div>
    <div class="draw-grid" id="drawGridContainer"></div>
    <div class="draw-progress" id="drawProgress">0/20</div>
    <button class="draw-result-btn" id="showResultBtn" onclick="showFinalResult()">SHOW THE RESULTS</button>
    <button class="back-btn" id="backToGameBtn" onclick="backToGame()">Back to Game</button>
    <div class="fairness-footer">
      <span>FAIRNESS</span><br>
      <span>ATLAS-V GAMING</span>
    </div>
  </div>

  <div class="tabs">
    <span class="tab active" onclick="switchTab('game')">GAME</span>
    <span class="tab" onclick="switchTab('history')">HISTORY</span>
    <span class="tab" onclick="switchTab('results')">RESULTS</span>
    <span class="tab" onclick="switchTab('stats')">ST.</span>
  </div>

  <div id="tab-history" class="tab-content" style="margin-top:8px;">
    <div id="historyContent">Loading...</div>
  </div>
  <div id="tab-results" class="tab-content" style="margin-top:8px;">
    <table class="leader-table">
      <thead><tr><th>#</th><th>ID</th><th>Bet</th><th>Win</th></tr></thead>
      <tbody id="leaderboardBody"></tbody>
    </table>
  </div>
  <div id="tab-stats" class="tab-content" style="margin-top:8px;">
    <div class="stats-table" id="statsContainer"></div>
  </div>
</div>

<script>
const tg = window.Telegram.WebApp; tg.expand();
const socket = io();
const userId = tg.initDataUnsafe?.user?.id || 123456;
let balance = 0, roundRemaining = 60, roundActive = true, roundId = 1;
let selected = new Set(), myTickets = [], betAmount = 2, currentDrawn = [], currentWin = 0, drawInProgress = false;

function updateBal() {
  document.getElementById('balanceDisplay').innerText = balance.toFixed(2) + ' ETB';
}

socket.on('connect', () => {
  socket.emit('request_balance', {user_id: userId});
  socket.emit('request_tickets', {user_id: userId});
});
socket.on('balance', (d) => {
  balance = d.balance; updateBal();
  if (betAmount > balance) betAmount = balance;
  document.getElementById('betAmountDisplay').innerText = betAmount;
});
socket.on('round_state', (d) => {
  roundRemaining = d.remaining; roundId = d.round_id;
  document.getElementById('roundId').innerText = `ID: ${roundId}`;
  updateTimer();
  if (d.drawn) { showDraw(d.drawn, d.winners || {}); }
  else { hideDraw(); roundActive = true; renderGrid(); }
});
socket.on('new_round', (d) => {
  roundId = d.round_id; roundRemaining = d.duration; roundActive = true;
  selected.clear(); myTickets = []; updateTickets();
  hideDraw(); renderGrid();
  document.getElementById('roundId').innerText = `ID: ${roundId}`;
  updateTimer(); drawInProgress = false;
  switchTab('game');
});
socket.on('draw_result', (d) => { roundActive = false; showDraw(d.drawn, d.winners); });
socket.on('bet_success', (d) => {
  myTickets = d.tickets; balance = d.balance; updateBal();
  selected.clear(); renderGrid(); updateTickets();
});
socket.on('your_tickets', (d) => { myTickets = d.tickets; updateTickets(); });
socket.on('error', (d) => alert(d.message));

setInterval(() => {
  if (roundRemaining > 0) { roundRemaining--; updateTimer(); if (roundRemaining <= 0) roundActive = false; }
}, 1000);

function updateTimer() {
  const m = Math.floor(roundRemaining/60), s = roundRemaining%60;
  document.getElementById('timerDisplay').innerText = `${m.toString().padStart(2,'0')}:${s.toString().padStart(2,'0')}`;
}

function renderGrid() {
  const g = document.getElementById('numberGrid'); g.innerHTML = '';
  for (let i=1; i<=80; i++) {
    const d = document.createElement('div');
    d.className = 'num' + (selected.has(i) ? ' selected' : '');
    d.innerText = i; d.onclick = () => toggleNum(i);
    g.appendChild(d);
  }
}

function toggleNum(n) {
  if (!roundActive || drawInProgress) return;
  if (selected.has(n)) selected.delete(n);
  else { if (selected.size >= 10) { alert('Max 10 numbers'); return; } selected.add(n); }
  renderGrid();
}

function adjustBet(d) {
  let nb = betAmount + d; if (nb < 1) nb = 1; if (nb > balance) nb = balance;
  betAmount = nb; document.getElementById('betAmountDisplay').innerText = betAmount;
}
function setBet(v) {
  betAmount = Math.min(v, balance); if (betAmount < 1) betAmount = 1;
  document.getElementById('betAmountDisplay').innerText = betAmount;
}

function placeTicket() {
  if (!roundActive) return alert('Round not active');
  if (selected.size === 0) return alert('Pick at least 1 number');
  if (myTickets.length >= 5) return alert('Max 5 tickets');
  if (betAmount > balance) return alert('Insufficient balance');
  socket.emit('place_bet', { user_id: userId, numbers: Array.from(selected), amount: betAmount });
}

function updateTickets() {
  const a = document.getElementById('ticketArea'), l = document.getElementById('ticketList');
  if (myTickets.length === 0) { a.style.display = 'none'; return; }
  a.style.display = 'block';
  l.innerHTML = myTickets.map(t =>
    `<div class="ticket-item">
      <span class="ticket-id">${t.mask}</span>
      <span class="ticket-nums">${t.numbers.join(' ')}</span>
      <span class="ticket-amount">${t.amount.toFixed(2)} ETB</span>
      <span class="ticket-status">Waiting</span>
    </div>`
  ).join('');
}

function switchTab(tab) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
  if (tab === 'game') {
    document.querySelectorAll('.tab')[0].classList.add('active');
    document.getElementById('pickerArea').style.display = drawInProgress ? 'none' : 'block';
    document.getElementById('drawArea').style.display = drawInProgress ? 'block' : 'none';
  } else if (tab === 'history') {
    document.querySelectorAll('.tab')[1].classList.add('active');
    hideBoth(); document.getElementById('tab-history').classList.add('active');
    socket.emit('request_history', {user_id: userId});
  } else if (tab === 'results') {
    document.querySelectorAll('.tab')[2].classList.add('active');
    hideBoth(); document.getElementById('tab-results').classList.add('active');
    socket.emit('request_leaderboard');
  } else if (tab === 'stats') {
    document.querySelectorAll('.tab')[3].classList.add('active');
    hideBoth(); document.getElementById('tab-stats').classList.add('active');
    socket.emit('request_stats');
  }
}

function hideBoth() {
  document.getElementById('pickerArea').style.display = 'none';
  document.getElementById('drawArea').style.display = 'none';
}

socket.on('history_data', (d) => {
  let h = d.history.length === 0 ? '<p style="color:#8899aa;">No history yet.</p>' :
    d.history.map(h => `<div class="history-item"><b>Round ${h.round_id}</b> - ${h.mask}<br>Numbers: ${h.numbers.join(' ')} | Bet: ${h.amount.toFixed(2)} ETB<br>Win: ${h.win.toFixed(2)} ETB</div>`).join('');
  document.getElementById('historyContent').innerHTML = h;
});
socket.on('leaderboard_data', (d) => {
  document.getElementById('leaderboardBody').innerHTML = d.leaders.length === 0 ?
    '<tr><td colspan="4" style="color:#8899aa;">No big wins yet.</td></tr>' :
    d.leaders.map((l,i) => `<tr><td>${i+1}</td><td>${l.mask}</td><td>${l.bet}</td><td>${l.win.toFixed(2)} ETB</td></tr>`).join('');
});
socket.on('stats_data', (d) => {
  let html = '';
  for (let dec=1; dec<=80; dec+=10) {
    let nums='', counts='';
    for (let i=dec; i<dec+10; i++) {
      nums += `<div class="stats-cell">${i}</div>`;
      counts += `<div class="stats-cell"><span class="freq">${d.freq[i-1]}</span></div>`;
    }
    html += nums + counts;
  }
  document.getElementById('statsContainer').innerHTML = html;
});

function showDraw(drawn, winners) {
  drawInProgress = true; currentDrawn = drawn; currentWin = winners[userId] || 0;
  document.getElementById('pickerArea').style.display = 'none';
  document.getElementById('drawArea').style.display = 'block';
  switchTab('game');
  document.getElementById('drawRoundId').innerText = `ID: ${roundId}`;
  const c = document.getElementById('drawGridContainer'); c.innerHTML = '';
  for (let row=0; row<3; row++) {
    const rd = document.createElement('div'); rd.className = 'draw-row' + (row===2 ? ' draw-extra-row' : '');
    const start = row*9, end = row===2 ? 20 : start+9;
    for (let i=start; i<end; i++) {
      const n = document.createElement('div'); n.className = 'draw-num'; n.id = 'dn'+i; rd.appendChild(n);
    }
    c.appendChild(rd);
  }
  document.getElementById('drawProgress').innerText = '0/20';
  document.getElementById('showResultBtn').style.display = 'none';
  document.getElementById('backToGameBtn').style.display = 'none';
  let idx = 0;
  (function reveal() {
    if (idx < 20) {
      const el = document.getElementById('dn'+idx);
      if (el) { el.innerText = drawn[idx]; el.classList.add('show'); }
      document.getElementById('drawProgress').innerText = (idx+1)+'/20';
      idx++; setTimeout(reveal, 300);
    } else {
      document.getElementById('showResultBtn').style.display = 'block';
    }
  })();
}

function hideDraw() {
  drawInProgress = false;
  document.getElementById('drawArea').style.display = 'none';
  document.getElementById('pickerArea').style.display = 'block';
  document.getElementById('showResultBtn').style.display = 'none';
  document.getElementById('backToGameBtn').style.display = 'none';
}

function showFinalResult() {
  document.getElementById('showResultBtn').style.display = 'none';
  document.getElementById('backToGameBtn').style.display = 'block';
  if (myTickets.length > 0) alert(`You won ${currentWin.toFixed(2)} ETB!`);
  else alert('You did not place any tickets.');
  balance += currentWin; updateBal();
}

function backToGame() {
  hideDraw();
  switchTab('game');
}

function deposit() {
  const amt = parseFloat(prompt('Deposit amount (ETB):'));
  if (amt && amt>0) socket.emit('deposit', {user_id: userId, amount: amt});
}

renderGrid();
document.getElementById('roundId').innerText = `ID: ${roundId}`;
document.getElementById('pickerArea').style.display = 'block';
document.getElementById('drawArea').style.display = 'none';
</script>
</body>
</html>'''

@app.route('/')
def index():
    return Response(HTML, mimetype='text/html')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.start_background_task(round_loop)
    socketio.run(app, host='0.0.0.0', port=port)
