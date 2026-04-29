import eventlet
eventlet.monkey_patch()

import random, time, sqlite3, os, string
from flask import Flask, Response
from flask_socketio import SocketIO, emit, join_room

app = Flask(__name__)
app.config['SECRET_KEY'] = 'lite-games-clone'
socketio = SocketIO(app, async_mode='eventlet')

# ---------- Database ----------
conn = sqlite3.connect("keno_app.db", check_same_thread=False)
conn.execute("""CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY, balance REAL DEFAULT 100.0
)""")
conn.execute("""CREATE TABLE IF NOT EXISTS rounds (
    id INTEGER PRIMARY KEY,
    drawn_numbers TEXT,
    timestamp REAL
)""")
conn.execute("""CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    round_id INTEGER,
    numbers TEXT,
    amount REAL,
    ticket_mask TEXT,
    win_amount REAL DEFAULT 0
)""")
conn.commit()

# ---------- Payouts ----------
PAYTABLE = {
    1: {0:0, 1:2},
    2: {0:0, 1:0, 2:4},
    3: {0:0, 1:0, 2:2, 3:12},
    4: {0:0, 1:0, 2:1, 3:5, 4:30},
    5: {0:0, 1:0, 2:0, 3:4, 4:15, 5:80},
    6: {0:0, 1:0, 2:0, 3:3, 4:10, 5:50, 6:200},
    7: {0:0, 1:0, 2:0, 3:2, 4:5, 5:20, 6:100, 7:500},
    8: {0:0, 1:0, 2:0, 3:1, 4:3, 5:10, 6:40, 7:200, 8:1000},
    9: {0:0, 1:0, 2:0, 3:1, 4:2, 5:8, 6:30, 7:150, 8:600, 9:2000},
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
    drawn = sorted(random.sample(range(1,81), 20))
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
            conn.execute("""INSERT INTO tickets (user_id, round_id, numbers, amount, ticket_mask, win_amount)
                           VALUES (?,?,?,?,?,?)""",
                         (uid, round_obj.id, ','.join(map(str,t['numbers'])),
                          t['amount'], t['mask'], win))
            conn.commit()
        cur = conn.cursor()
        cur.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (total_win, uid))
        conn.commit()
        winners[uid] = total_win
    return drawn, winners

# ---------- Socket.IO ----------
@socketio.on('connect')
def on_connect():
    join_room('round')
    elapsed = time.time() - current_round.start_time
    remaining = max(0, current_round.timer - elapsed)
    emit('round_state', {'round_id':current_round.id, 'remaining':remaining, 'drawn':current_round.drawn})

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
    emit('bet_success', {
        'tickets': [{'mask':t['mask'],'numbers':t['numbers'],'amount':t['amount']} for t in tickets],
        'balance': get_balance(uid)
    })

@socketio.on('request_tickets')
def req_tickets(data):
    uid = data['user_id']
    tickets = current_round.bets.get(uid,[])
    emit('your_tickets', {'tickets': [{'mask':t['mask'],'numbers':t['numbers'],'amount':t['amount']} for t in tickets]})

@socketio.on('request_history')
def hist(data):
    uid = data['user_id']
    rows = conn.execute("""SELECT t.round_id, t.numbers, t.amount, t.ticket_mask, t.win_amount, r.drawn_numbers
                          FROM tickets t JOIN rounds r ON t.round_id=r.id
                          WHERE t.user_id=? ORDER BY t.round_id DESC LIMIT 50""", (uid,)).fetchall()
    out = []
    for r in rows:
        out.append({'round_id':r[0],'numbers':list(map(int,r[1].split(','))),'amount':r[2],'mask':r[3],'win':r[4],
                    'drawn':list(map(int,r[5].split(','))) if r[5] else []})
    emit('history_data', {'history':out})

@socketio.on('request_leaderboard')
def leaderboard():
    rows = conn.execute("""SELECT ticket_mask, amount, win_amount FROM tickets
                           WHERE win_amount>0 ORDER BY win_amount DESC LIMIT 10""").fetchall()
    emit('leaderboard_data', {'leaders':[{'mask':r[0],'bet':r[1],'win':r[2]} for r in rows]})

@socketio.on('request_stats')
def stats():
    rows = conn.execute("SELECT drawn_numbers FROM rounds ORDER BY id DESC LIMIT 100").fetchall()
    freq = [0]*81
    for r in rows:
        for n in map(int, r[0].split(',')):
            if 1<=n<=80: freq[n] += 1
    emit('stats_data', {'freq': freq[1:]})

# ---------- Round loop ----------
def round_loop():
    global current_round
    while True:
        socketio.sleep(current_round.timer)
        drawn, winners = process_draw(current_round)
        socketio.emit('draw_result', {'round_id':current_round.id, 'drawn':drawn, 'winners':winners}, room='round')
        current_round = Round(current_round.id+1)
        socketio.emit('new_round', {'round_id':current_round.id, 'duration':current_round.timer}, room='round')

# ---------- HTML (Integrated draw, exact colors) ----------
HTML = r'''<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <script src="https://cdn.socket.io/4.5.0/socket.io.min.js"></script>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0b0f14; color: #d1d4d8; padding: 16px 10px;
            min-height: 100vh; user-select: none; -webkit-tap-highlight-color: transparent;
        }
        .top-bar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
        .balance { background: #152028; padding: 8px 16px; border-radius: 20px; font-weight: 600; font-size: 15px; color: #fff; }
        .deposit-btn { background: #2e7d32; color: white; border: none; padding: 8px 16px; border-radius: 20px; font-size: 15px; font-weight: 600; cursor: pointer; }
        .round-id { font-size: 13px; color: #8899aa; margin-bottom: 4px; text-align: left; }
        .timer { font-size: 34px; font-weight: 700; color: #ffaa00; margin: 6px 0 10px; text-align: center; }
        .circles { display: flex; justify-content: space-between; margin: 0 0 8px; }
        .circle { width: 36px; height: 36px; border-radius: 50%; background: #1c2636; display: flex; align-items: center; justify-content: center; font-weight: bold; font-size: 17px; color: #aaa; border: 2px solid #3a4a5a; }
        .picker-info { font-size: 14px; color: #8899aa; margin: 0 0 2px; text-align: center; }
        .picker-sub { font-size: 12px; color: #667788; margin-bottom: 10px; text-align: center; }
        .grid { display: grid; grid-template-columns: repeat(10, 1fr); gap: 5px; margin: 10px 0; }
        .num { background: #1c2636; border-radius: 6px; padding: 14px 0; font-size: 15px; font-weight: 500; text-align: center; cursor: pointer; color: #c0c8d0; transition: background 0.1s; }
        .num.selected { background: #4caf50; color: white; font-weight: bold; }
        .bet-controls { display: flex; align-items: center; justify-content: center; margin: 18px 0; gap: 8px; }
        .bet-amount { font-size: 22px; font-weight: bold; min-width: 40px; text-align: center; color: white; }
        .btn-sm { background: #2a3a4a; border: none; color: white; font-size: 22px; width: 36px; height: 36px; border-radius: 6px; cursor: pointer; line-height: 36px; text-align: center; }
        .btn-x2, .btn-max { background: #e55300; color: white; border: none; padding: 10px 14px; border-radius: 6px; font-weight: bold; font-size: 16px; cursor: pointer; }
        .place-bet-btn { background: #e55300; color: white; border: none; padding: 16px; border-radius: 8px; font-size: 20px; font-weight: bold; width: 100%; margin: 10px 0; cursor: pointer; text-transform: uppercase; letter-spacing: 1px; }
        .ticket-area { margin: 10px 0; display: none; }
        .ticket-label { font-size: 14px; color: #8899aa; margin-bottom: 6px; text-align: left; }
        .ticket-list { background: #131c26; border-radius: 8px; padding: 10px; }
        .ticket-item { display: flex; justify-content: space-between; font-size: 14px; padding: 6px 0; border-bottom: 1px solid #233045; }
        .ticket-item:last-child { border-bottom: none; }
        .ticket-id { font-family: monospace; color: #ccc; width: 60px; }
        .ticket-nums { font-family: monospace; color: #aaa; flex: 1; padding-left: 8px; }
        .ticket-amount { min-width: 70px; text-align: right; }
        .ticket-status { color: #d4a017; font-weight: 500; min-width: 60px; text-align: right; }   /* soft yellow */
        .tabs { display: flex; justify-content: space-around; background: #152028; padding: 12px 0; border-radius: 10px; margin-top: 15px; font-size: 14px; color: #667788; font-weight: 600; }
        .tab { cursor: pointer; padding: 0 4px; }
        .tab.active { color: #4caf50; }

        /* Draw area (inside game screen) */
        #drawArea { display: none; }
        .draw-id { font-size: 13px; color: #8899aa; margin-bottom: 20px; }
        .draw-grid { margin: 20px 0; display: flex; flex-wrap: wrap; justify-content: center; }
        .draw-row { display: flex; justify-content: center; gap: 6px; margin-bottom: 6px; width: 100%; }
        .draw-num { background: #1c2636; width: 34px; height: 38px; display: flex; align-items: center; justify-content: center; border-radius: 5px; font-size: 18px; font-weight: 600; color: transparent; transition: all 0.2s; }
        .draw-num.show { background: #ed4452; color: white; }
        .draw-extra-row { justify-content: center; gap: 16px; }
        .draw-progress { font-size: 20px; margin: 12px 0; color: #ffaa00; text-align: center; }
        .draw-result-btn { background: #2e7d32; color: white; border: none; padding: 14px; border-radius: 8px; font-size: 18px; font-weight: bold; width: 100%; cursor: pointer; margin-top: 20px; display: none; }
        .back-btn { background: #2e7d32; color: white; border: none; padding: 14px; border-radius: 8px; font-size: 18px; font-weight: bold; width: 100%; cursor: pointer; margin-top: 15px; display: none; }
        .fairness-footer { text-align: center; margin-top: 20px; padding-top: 15px; border-top: 1px solid #2a3a4a; font-size: 13px; color: #667788; }
        .tab-content { display: none; }
        .tab-content.active { display: block; }

        /* Leaderboard table */
        .leader-table { width: 100%; border-collapse: collapse; margin-top: 10px; }
        .leader-table th, .leader-table td { padding: 8px 4px; font-size: 14px; border-bottom: 1px solid #233045; text-align: left; color: #d1d4d8; }
        .leader-table th { color: #8899aa; font-weight: 500; }

        /* Stats grid */
        .stats-table { display: grid; grid-template-columns: repeat(10, 1fr); gap: 4px; margin: 10px 0; }
        .stats-cell { background: #1c2636; border-radius: 4px; padding: 8px 0; text-align: center; font-size: 14px; color: #c0c8d0; }
        .stats-cell .freq { font-size: 18px; font-weight: bold; color: #ffffff; } /* white, not orange */

        .history-item { background: #131c26; border-radius: 8px; padding: 10px; margin-bottom: 8px; font-size: 14px; }
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

        <!-- Dynamic content area (picker, draw, or tabs content) -->
        <div id="pickerArea">
            <div class="picker-info">Choose 10 numbers</div>
            <div class="picker-sub">From 1 to 80</div>
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
                <span>FAIRNESS</span>
                <span>ATLAS-V GAMING</span>
            </div>
        </div>

        <!-- Tabs -->
        <div class="tabs">
            <span class="tab active" onclick="switchTab('game')">GAME</span>
            <span class="tab" onclick="switchTab('history')">HISTORY</span>
            <span class="tab" onclick="switchTab('results')">RESULTS</span>
            <span class="tab" onclick="switchTab('stats')">ST.</span>
        </div>

        <!-- Tab contents for HISTORY, RESULTS, STATS -->
        <div id="tab-history" class="tab-content" style="margin-top:10px;">
            <div id="historyContent">Loading...</div>
        </div>
        <div id="tab-results" class="tab-content" style="margin-top:10px;">
            <table class="leader-table">
                <thead><tr><th>#</th><th>ID</th><th>Bet</th><th>Win</th></tr></thead>
                <tbody id="leaderboardBody"></tbody>
            </table>
        </div>
        <div id="tab-stats" class="tab-content" style="margin-top:10px;">
            <div class="stats-table" id="statsContainer"></div>
        </div>
    </div>

    <script>
        const tg = window.Telegram.WebApp;
        tg.expand();
        const socket = io();
        const userId = tg.initDataUnsafe?.user?.id || 123456;
        let balance = 0, roundRemaining = 60, roundActive = true, roundId = 1;
        let selected = new Set();
        let myTickets = [];
        let betAmount = 2;
        let currentDrawn = [], currentWin = 0;
        let drawInProgress = false;   // true when draw is being shown

        function updateBalanceDisplay() {
            document.getElementById('balanceDisplay').innerText = balance.toFixed(2) + ' ETB';
        }

        // Socket events
        socket.on('connect', () => {
            socket.emit('request_balance', {user_id: userId});
            socket.emit('request_tickets', {user_id: userId});
        });
        socket.on('balance', (data) => {
            balance = data.balance;
            updateBalanceDisplay();
            if (betAmount > balance) betAmount = balance;
            updateBetDisplay();
        });
        socket.on('round_state', (data) => {
            roundRemaining = data.remaining;
            roundId = data.round_id;
            document.getElementById('roundId').innerText = `ID: ${roundId}`;
            updateTimer();
            if (data.drawn) {
                // Show draw immediately
                showDrawArea(data.drawn, data.winners || {});
            } else {
                hideDrawArea();
                roundActive = true;
                renderGrid();
            }
        });
        socket.on('new_round', (data) => {
            roundId = data.round_id;
            roundRemaining = data.duration;
            roundActive = true;
            selected.clear();
            myTickets = [];
            updateTicketUI();
            hideDrawArea();
            renderGrid();
            document.getElementById('roundId').innerText = `ID: ${roundId}`;
            updateTimer();
            drawInProgress = false;
            // Switch to GAME tab automatically
            switchTab('game');
        });
        socket.on('draw_result', (data) => {
            roundActive = false;
            showDrawArea(data.drawn, data.winners);
        });
        socket.on('bet_success', (data) => {
            myTickets = data.tickets;
            balance = data.balance;
            updateBalanceDisplay();
            selected.clear();
            renderGrid();
            updateTicketUI();
        });
        socket.on('your_tickets', (data) => { myTickets = data.tickets; updateTicketUI(); });
        socket.on('error', (data) => { alert(data.message); });

        // Timer countdown
        setInterval(() => {
            if (roundRemaining > 0) {
                roundRemaining--;
                updateTimer();
                if (roundRemaining <= 0) roundActive = false;
            }
        }, 1000);

        function updateTimer() {
            const m = Math.floor(roundRemaining/60), s = roundRemaining%60;
            document.getElementById('timerDisplay').innerText = `${m.toString().padStart(2,'0')}:${s.toString().padStart(2,'0')}`;
        }

        // Grid rendering
        function renderGrid() {
            const grid = document.getElementById('numberGrid');
            grid.innerHTML = '';
            for (let i=1; i<=80; i++) {
                const div = document.createElement('div');
                div.className = 'num' + (selected.has(i) ? ' selected' : '');
                div.innerText = i;
                div.onclick = () => toggleNum(i);
                grid.appendChild(div);
            }
        }

        function toggleNum(n) {
            if (!roundActive || drawInProgress) return;
            if (selected.has(n)) selected.delete(n);
            else {
                if (selected.size >= 10) { alert('Max 10 numbers'); return; }
                selected.add(n);
            }
            renderGrid();
        }

        // Bet controls
        function adjustBet(delta) {
            let newBet = betAmount + delta;
            if (newBet < 1) newBet = 1;
            if (newBet > balance) newBet = balance;
            betAmount = newBet;
            updateBetDisplay();
        }
        function setBet(val) {
            betAmount = Math.min(val, balance);
            if (betAmount < 1) betAmount = 1;
            updateBetDisplay();
        }
        function updateBetDisplay() { document.getElementById('betAmountDisplay').innerText = betAmount; }

        function placeTicket() {
            if (!roundActive) return alert('Round not active');
            if (selected.size === 0) return alert('Pick at least 1 number');
            if (myTickets.length >= 5) return alert('Max 5 tickets');
            if (betAmount > balance) return alert('Insufficient balance');
            socket.emit('place_bet', { user_id: userId, numbers: Array.from(selected), amount: betAmount });
        }

        function updateTicketUI() {
            const area = document.getElementById('ticketArea');
            const list = document.getElementById('ticketList');
            if (myTickets.length === 0) { area.style.display = 'none'; return; }
            area.style.display = 'block';
            list.innerHTML = myTickets.map(t =>
                `<div class="ticket-item">
                    <span class="ticket-id">${t.mask}</span>
                    <span class="ticket-nums">${t.numbers.join(' ')}</span>
                    <span class="ticket-amount">${t.amount.toFixed(2)} ETB</span>
                    <span class="ticket-status">Waiting</span>
                </div>`).join('');
        }

        // Tab switching
        function switchTab(tab) {
            const tabs = document.querySelectorAll('.tab');
            tabs.forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));

            if (tab === 'game') {
                tabs[0].classList.add('active');
                // If a draw is active, show draw area; else show picker
                if (drawInProgress) {
                    document.getElementById('pickerArea').style.display = 'none';
                    document.getElementById('drawArea').style.display = 'block';
                } else {
                    document.getElementById('pickerArea').style.display = 'block';
                    document.getElementById('drawArea').style.display = 'none';
                }
                // Hide other tab contents
                document.getElementById('tab-history').classList.remove('active');
                document.getElementById('tab-results').classList.remove('active');
                document.getElementById('tab-stats').classList.remove('active');
            } else if (tab === 'history') {
                tabs[1].classList.add('active');
                hidePickerAndDraw();
                document.getElementById('tab-history').classList.add('active');
                loadHistory();
            } else if (tab === 'results') {
                tabs[2].classList.add('active');
                hidePickerAndDraw();
                document.getElementById('tab-results').classList.add('active');
                loadLeaderboard();
            } else if (tab === 'stats') {
                tabs[3].classList.add('active');
                hidePickerAndDraw();
                document.getElementById('tab-stats').classList.add('active');
                loadStats();
            }
        }

        function hidePickerAndDraw() {
            document.getElementById('pickerArea').style.display = 'none';
            document.getElementById('drawArea').style.display = 'none';
        }

        // History, leaderboard, stats loaders
        function loadHistory() { socket.emit('request_history', {user_id: userId}); }
        socket.on('history_data', (data) => {
            let html = '';
            if (data.history.length === 0) html = '<p style="color:#8899aa;">No history yet.</p>';
            else {
                data.history.forEach(h => {
                    html += `<div class="history-item">
                        <b>Round ${h.round_id}</b> - ${h.mask}<br>
                        Numbers: ${h.numbers.join(' ')} | Bet: ${h.amount.toFixed(2)} ETB<br>
                        Win: ${h.win.toFixed(2)} ETB
                    </div>`;
                });
            }
            document.getElementById('historyContent').innerHTML = html;
        });

        function loadLeaderboard() { socket.emit('request_leaderboard'); }
        socket.on('leaderboard_data', (data) => {
            let html = '';
            data.leaders.forEach((l, i) => {
                html += `<tr>
                    <td>${i+1}</td>
                    <td>${l.mask}</td>
                    <td>${l.bet}</td>
                    <td>${l.win.toFixed(2)} ETB</td>
                </tr>`;
            });
            document.getElementById('leaderboardBody').innerHTML = html || '<tr><td colspan="4" style="color:#8899aa;">No big wins yet.</td></tr>';
        });

        function loadStats() { socket.emit('request_stats'); }
        socket.on('stats_data', (data) => {
            const freq = data.freq;
            let html = '';
            for (let decade = 1; decade <= 80; decade += 10) {
                let nums = '', counts = '';
                for (let i = decade; i < decade+10; i++) {
                    nums += `<div class="stats-cell">${i}</div>`;
                    counts += `<div class="stats-cell"><span class="freq">${freq[i-1]}</span></div>`;
                }
                html += nums + counts;
            }
            document.getElementById('statsContainer').innerHTML = html;
        });

        // Draw area management
        function showDrawArea(drawn, winners) {
            drawInProgress = true;
            currentDrawn = drawn;
            currentWin = winners[userId] || 0;

            // Hide picker, show draw
            document.getElementById('pickerArea').style.display = 'none';
            document.getElementById('drawArea').style.display = 'block';
            // Ensure we are on GAME tab
            switchTab('game'); // will set active tab and show draw area

            document.getElementById('drawRoundId').innerText = `ID: ${roundId}`;
            const container = document.getElementById('drawGridContainer');
            container.innerHTML = '';
            const row1 = document.createElement('div'); row1.className = 'draw-row';
            for (let i=0; i<9; i++) { const d = document.createElement('div'); d.className = 'draw-num'; d.id = 'dn'+i; row1.appendChild(d); }
            const row2 = document.createElement('div'); row2.className = 'draw-row';
            for (let i=9; i<18; i++) { const d = document.createElement('div'); d.className = 'draw-num'; d.id = 'dn'+i; row2.appendChild(d); }
            const row3 = document.createElement('div'); row3.className = 'draw-row draw-extra-row';
            for (let i=18; i<20; i++) { const d = document.createElement('div'); d.className = 'draw-num'; d.id = 'dn'+i; row3.appendChild(d); }
            container.appendChild(row1); container.appendChild(row2); container.appendChild(row3);

            document.getElementById('drawProgress').innerText = '0/20';
            document.getElementById('showResultBtn').style.display = 'none';
            document.getElementById('backToGameBtn').style.display = 'none';

            let index = 0;
            function reveal() {
                if (index < 20) {
                    const el = document.getElementById('dn'+index);
                    if (el) { el.innerText = drawn[index]; el.classList.add('show'); }
                    document.getElementById('drawProgress').innerText = (index+1)+'/20';
                    index++;
                    setTimeout(reveal, 300);
                } else {
                    document.getElementById('showResultBtn').style.display = 'block';
                }
            }
            reveal();
        }

        function hideDrawArea() {
            drawInProgress = false;
            document.getElementById('drawArea').style.display = 'none';
            document.getElementById('pickerArea').style.display = 'block';
            // Also hide result buttons
            document.getElementById('showResultBtn').style.display = 'none';
            document.getElementById('backToGameBtn').style.display = 'none';
        }

        function showFinalResult() {
            document.getElementById('showResultBtn').style.display = 'none';
            document.getElementById('backToGameBtn').style.display = 'block';
            if (myTickets.length > 0) {
                alert(`You won ${currentWin.toFixed(2)} ETB!`);
            } else {
                alert('You did not place any tickets.');
            }
            balance += currentWin;
            updateBalanceDisplay();
        }

        function backToGame() {
            // This will just hide the draw area and show picker, but the round is still over.
            // The new_round event will soon reset everything, but we can manually hide.
            hideDrawArea();
            // Switch to GAME tab
            switchTab('game');
        }

        function deposit() {
            const amt = parseFloat(prompt('Enter deposit amount (ETB):'));
            if (amt && amt>0) socket.emit('deposit', {user_id: userId, amount: amt});
        }

        // Initial setup
        updateBetDisplay();
        renderGrid();
        document.getElementById('roundId').innerText = `ID: ${roundId}`;
        // Start with picker visible
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
