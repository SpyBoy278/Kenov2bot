import eventlet
eventlet.monkey_patch()

import random
import time
import sqlite3
import os
import string
from flask import Flask, Response
from flask_socketio import SocketIO, emit, join_room

app = Flask(__name__)
app.config['SECRET_KEY'] = 'change-me-to-a-random-string'
socketio = SocketIO(app, async_mode='eventlet')

# ---------- Database ----------
conn = sqlite3.connect("keno_app.db", check_same_thread=False)
conn.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, balance REAL DEFAULT 100.0)")
conn.commit()

# ---------- Game State ----------
class Round:
    def __init__(self, rid):
        self.id = rid
        self.start_time = time.time()
        self.bets = {}
        self.drawn = None
        self.timer = 60

current_round = Round(1)

PAYTABLE = {0:0,1:0,2:0,3:1,4:2,5:5,6:25,7:100,8:500,9:1000,10:5000}

def generate_ticket_id():
    chars = string.digits
    rid = ''.join(random.choices(chars, k=4))
    masked = rid[0] + '***' + rid[3]
    return masked

def process_draw(round_obj):
    drawn = sorted(random.sample(range(1, 81), 20))
    round_obj.drawn = drawn
    winners = {}
    for uid, tickets in round_obj.bets.items():
        total_win = 0
        for t in tickets:
            matches = len(set(t['numbers']) & set(drawn))
            mult = PAYTABLE.get(matches, 0)
            total_win += t['amount'] * mult
        if total_win > 0:
            cur = conn.cursor()
            cur.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (total_win, uid))
            conn.commit()
        winners[uid] = total_win
    return drawn, winners

# ---------- Socket.IO Handlers ----------
@socketio.on('connect')
def handle_connect():
    join_room('round')
    elapsed = time.time() - current_round.start_time
    remaining = max(0, current_round.timer - elapsed)
    emit('round_state', {'round_id': current_round.id, 'remaining': remaining, 'drawn': current_round.drawn})

def get_balance(uid):
    cur = conn.cursor()
    cur.execute("SELECT balance FROM users WHERE user_id = ?", (uid,))
    row = cur.fetchone()
    return row[0] if row else 100.0

@socketio.on('request_balance')
def handle_balance(data):
    user_id = data['user_id']
    bal = get_balance(user_id)
    emit('balance', {'balance': bal})

@socketio.on('deposit')
def handle_deposit(data):
    user_id = data['user_id']
    amount = float(data['amount'])
    if amount <= 0:
        emit('error', {'message': 'Invalid amount'})
        return
    cur = conn.cursor()
    cur.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    emit('balance', {'balance': get_balance(user_id)})

@socketio.on('place_bet')
def handle_bet(data):
    user_id = data['user_id']
    numbers = data['numbers']
    amount = float(data['amount'])
    if len(numbers) != 10:
        emit('error', {'message': 'Pick exactly 10 numbers'})
        return
    if amount > get_balance(user_id):
        emit('error', {'message': 'Insufficient balance'})
        return
    if len(current_round.bets.get(user_id, [])) >= 5:
        emit('error', {'message': 'Max 5 tickets per round'})
        return
    cur = conn.cursor()
    cur.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    ticket_id = generate_ticket_id()
    ticket = {'numbers': numbers, 'amount': amount, 'ticket_id': ticket_id}
    current_round.bets.setdefault(user_id, []).append(ticket)
    emit('bet_success', {
        'tickets': [{'id': t['ticket_id'], 'amount': t['amount']} for t in current_round.bets[user_id]],
        'balance': get_balance(user_id)
    })

@socketio.on('request_tickets')
def handle_request_tickets(data):
    user_id = data['user_id']
    tickets = current_round.bets.get(user_id, [])
    emit('your_tickets', {'tickets': [{'id': t['ticket_id'], 'amount': t['amount']} for t in tickets]})

# ---------- Round Manager ----------
def round_loop():
    global current_round
    while True:
        socketio.sleep(current_round.timer)
        drawn, winners = process_draw(current_round)
        socketio.emit('draw_result', {
            'round_id': current_round.id,
            'drawn': drawn,
            'winners': winners
        }, room='round')
        current_round = Round(current_round.id + 1)
        socketio.emit('new_round', {
            'round_id': current_round.id,
            'duration': current_round.timer
        }, room='round')

# ---------- Embedded HTML (Pixel‑Perfect Clone) ----------
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
            background: #0b0f14; color: #d1d4d8;
            padding: 12px 8px;
            min-height: 100vh;
        }
        .top-bar {
            display: flex; justify-content: space-between; align-items: center;
            margin-bottom: 10px; font-size: 15px;
        }
        .balance {
            background: #182230; padding: 6px 14px; border-radius: 20px;
            font-weight: 600; color: white;
        }
        .deposit-btn {
            background: #2e7d32; color: white; border: none;
            padding: 6px 16px; border-radius: 20px; font-size: 15px;
            font-weight: 600; cursor: pointer;
        }
        .round-id {
            text-align: left; font-size: 13px; color: #8899aa; margin-bottom: 5px;
        }
        .timer {
            font-size: 34px; font-weight: 700; color: #ffaa00;
            margin: 8px 0; text-align: center;
        }
        .grid {
            display: grid; grid-template-columns: repeat(10, 1fr);
            gap: 4px; margin: 15px 5px; user-select: none;
        }
        .num {
            background: #1c2636; border-radius: 5px; padding: 14px 0;
            font-size: 15px; font-weight: 500; cursor: pointer;
            transition: background 0.1s; color: #c0c8d0;
        }
        .num.selected { background: #4caf50; color: white; }
        .ticket-area { margin: 10px 0; }
        .ticket-label {
            font-size: 14px; color: #8899aa; margin-bottom: 4px;
            text-align: left;
        }
        .ticket-list {
            background: #131c26; border-radius: 6px; padding: 8px 10px;
        }
        .ticket-item {
            display: flex; justify-content: space-between;
            font-size: 14px; padding: 4px 0;
            border-bottom: 1px solid #233045;
        }
        .ticket-item:last-child { border-bottom: none; }
        .ticket-id { font-family: monospace; color: #ccc; }
        .ticket-status { color: #ffaa00; font-weight: 500; }
        .place-btn {
            background: #e55300; color: white; border: none;
            padding: 16px; border-radius: 8px; font-size: 18px;
            font-weight: bold; width: 100%; margin: 12px 0; cursor: pointer;
        }
        .tabs {
            display: flex; justify-content: space-around;
            background: #152028; padding: 10px 0; border-radius: 8px;
            margin-top: 10px; font-size: 14px; color: #667788;
        }
        .tab { cursor: pointer; }
        .tab.active { color: #4caf50; font-weight: 600; }

        /* Draw Screen */
        .draw-screen { display: none; }
        .draw-top { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
        .draw-id { font-size: 13px; color: #8899aa; }
        .draw-grid {
            display: flex; flex-wrap: wrap; justify-content: center;
            margin: 20px 0;
        }
        .draw-row {
            width: 100%; display: flex; justify-content: center; gap: 5px;
            margin-bottom: 5px;
        }
        .draw-num {
            background: #1c2636; width: 32px; height: 36px;
            display: flex; align-items: center; justify-content: center;
            border-radius: 4px; font-size: 16px; font-weight: 600;
            color: transparent; transition: all 0.2s;
        }
        .draw-num.show { background: #ed4452; color: white; }
        .draw-extra-row { justify-content: center; gap: 16px; }
        .draw-progress { font-size: 18px; margin: 8px 0; color: #ffaa00; }
        .back-btn {
            background: #2e7d32; color: white; border: none;
            padding: 14px; border-radius: 8px; font-size: 16px;
            font-weight: bold; width: 100%; cursor: pointer; margin-top: 15px;
            display: none;
        }
    </style>
</head>
<body>
    <!-- Game Screen -->
    <div id="gameScreen">
        <div class="top-bar">
            <span class="balance" id="balanceDisplay">0.00 ETB</span>
            <button class="deposit-btn" onclick="deposit()">Deposit</button>
        </div>
        <div class="round-id" id="roundId">Round #1</div>
        <div class="timer" id="timerDisplay">00:60</div>

        <div class="grid" id="numberGrid"></div>

        <div class="ticket-area" id="ticketArea" style="display:none;">
            <div class="ticket-label">Your Tickets</div>
            <div class="ticket-list" id="ticketList"></div>
        </div>

        <button class="place-btn" id="placeTicketBtn" onclick="placeTicket()">Place Ticket (0/5)</button>

        <div class="tabs">
            <span class="tab active">GAME</span>
            <span class="tab">HISTORY</span>
            <span class="tab">RESULTS</span>
            <span class="tab">ST.</span>
        </div>
    </div>

    <!-- Draw Screen -->
    <div id="drawScreen" class="draw-screen">
        <div class="draw-top">
            <span class="balance" id="drawBalance">0.00 ETB</span>
            <button class="deposit-btn" onclick="deposit()">Deposit</button>
        </div>
        <div class="draw-id" id="drawRoundId">ID: 877020462</div>
        <div class="draw-grid" id="drawGridContainer"></div>
        <div class="draw-progress" id="drawProgress">0/20</div>
        <button class="back-btn" id="backToGameBtn" onclick="backToGame()">New Round</button>
    </div>

    <script>
        const tg = window.Telegram.WebApp;
        tg.expand();
        const socket = io();
        const userId = tg.initDataUnsafe?.user?.id || 123456;
        let selected = new Set();
        let balance = 0;
        let roundRemaining = 60;
        let roundActive = true;
        let roundId = 1;
        let myTickets = [];

        function updateBalanceDisplay() {
            document.getElementById('balanceDisplay').innerText = balance.toFixed(2) + ' ETB';
            document.getElementById('drawBalance').innerText = balance.toFixed(2) + ' ETB';
        }

        socket.on('connect', () => {
            socket.emit('request_balance', {user_id: userId});
            socket.emit('request_tickets', {user_id: userId});
        });

        socket.on('balance', (data) => {
            balance = data.balance;
            updateBalanceDisplay();
        });

        socket.on('round_state', (data) => {
            roundRemaining = data.remaining;
            roundId = data.round_id;
            document.getElementById('roundId').innerText = `ID: ${roundId}`;
            updateTimer();
            if (data.drawn) {
                showDrawResults(data.drawn, data.winners || {});
            } else {
                document.getElementById('gameScreen').style.display = 'block';
                document.getElementById('drawScreen').style.display = 'none';
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
            renderGrid();
            document.getElementById('gameScreen').style.display = 'block';
            document.getElementById('drawScreen').style.display = 'none';
            document.getElementById('roundId').innerText = `ID: ${roundId}`;
            updateTimer();
        });

        socket.on('draw_result', (data) => {
            roundActive = false;
            let win = data.winners[userId] || 0;
            balance += win;
            updateBalanceDisplay();
            showDrawResults(data.drawn, data.winners);
        });

        socket.on('bet_success', (data) => {
            myTickets = data.tickets;
            balance = data.balance;
            updateBalanceDisplay();
            selected.clear();
            renderGrid();
            updateTicketUI();
            alert('Ticket placed!');
        });

        socket.on('your_tickets', (data) => {
            myTickets = data.tickets;
            updateTicketUI();
        });

        socket.on('error', (data) => {
            alert(data.message);
        });

        setInterval(() => {
            if (roundRemaining > 0) {
                roundRemaining--;
                updateTimer();
                if (roundRemaining <= 0) roundActive = false;
            }
        }, 1000);

        function updateTimer() {
            const m = Math.floor(roundRemaining / 60);
            const s = roundRemaining % 60;
            document.getElementById('timerDisplay').innerText = `${m.toString().padStart(2,'0')}:${s.toString().padStart(2,'0')}`;
        }

        function renderGrid() {
            const grid = document.getElementById('numberGrid');
            grid.innerHTML = '';
            for (let i = 1; i <= 80; i++) {
                const div = document.createElement('div');
                div.className = 'num' + (selected.has(i) ? ' selected' : '');
                div.innerText = i;
                div.onclick = () => toggleNumber(i);
                grid.appendChild(div);
            }
            document.getElementById('placeTicketBtn').innerText = `Place Ticket (${myTickets.length}/5)`;
        }

        function toggleNumber(num) {
            if (!roundActive) return;
            if (selected.has(num)) selected.delete(num);
            else {
                if (selected.size >= 10) return alert('Max 10 numbers');
                selected.add(num);
            }
            renderGrid();
        }

        function placeTicket() {
            if (!roundActive) return alert('Round not active');
            if (selected.size !== 10) return alert('Pick exactly 10 numbers');
            if (myTickets.length >= 5) return alert('Max 5 tickets');
            const betAmount = parseFloat(prompt('Enter bet amount (ETB):'));
            if (!betAmount || betAmount <= 0) return;
            if (betAmount > balance) return alert('Insufficient balance');
            socket.emit('place_bet', {
                user_id: userId,
                numbers: Array.from(selected),
                amount: betAmount
            });
        }

        function updateTicketUI() {
            const area = document.getElementById('ticketArea');
            const list = document.getElementById('ticketList');
            if (myTickets.length === 0) {
                area.style.display = 'none';
                return;
            }
            area.style.display = 'block';
            list.innerHTML = myTickets.map(t =>
                `<div class="ticket-item">
                    <span class="ticket-id">${t.id}</span>
                    <span>Bet ${t.amount.toFixed(2)} ETB</span>
                    <span class="ticket-status">Waiting</span>
                </div>`
            ).join('');
        }

        function showDrawResults(drawn, winners) {
            document.getElementById('gameScreen').style.display = 'none';
            document.getElementById('drawScreen').style.display = 'block';
            document.getElementById('drawRoundId').innerText = `ID: ${roundId}`;
            updateBalanceDisplay();

            // Build 9-9-2 layout
            const container = document.getElementById('drawGridContainer');
            container.innerHTML = '';
            // Row1: first 9 numbers
            const row1 = document.createElement('div');
            row1.className = 'draw-row';
            for (let i = 0; i < 9; i++) {
                const nDiv = document.createElement('div');
                nDiv.className = 'draw-num';
                nDiv.id = 'dn' + i;
                nDiv.innerText = drawn[i];
                row1.appendChild(nDiv);
            }
            container.appendChild(row1);
            // Row2: next 9 numbers
            const row2 = document.createElement('div');
            row2.className = 'draw-row';
            for (let i = 9; i < 18; i++) {
                const nDiv = document.createElement('div');
                nDiv.className = 'draw-num';
                nDiv.id = 'dn' + i;
                nDiv.innerText = drawn[i];
                row2.appendChild(nDiv);
            }
            container.appendChild(row2);
            // Row3: last 2 numbers centered
            const row3 = document.createElement('div');
            row3.className = 'draw-row draw-extra-row';
            for (let i = 18; i < 20; i++) {
                const nDiv = document.createElement('div');
                nDiv.className = 'draw-num';
                nDiv.id = 'dn' + i;
                nDiv.innerText = drawn[i];
                row3.appendChild(nDiv);
            }
            container.appendChild(row3);

            document.getElementById('backToGameBtn').style.display = 'none';
            document.getElementById('drawProgress').innerText = '0/20';

            // Animate reveal
            let index = 0;
            function revealNext() {
                if (index < 20) {
                    const el = document.getElementById('dn' + index);
                    if (el) el.classList.add('show');
                    document.getElementById('drawProgress').innerText = (index+1) + '/20';
                    index++;
                    setTimeout(revealNext, 300);
                } else {
                    document.getElementById('backToGameBtn').style.display = 'block';
                    const win = winners[userId] || 0;
                    if (myTickets.length > 0) {
                        alert(`Round ended! You won ${win.toFixed(2)} ETB`);
                    } else {
                        alert('Round ended. You did not place any tickets.');
                    }
                }
            }
            revealNext();
        }

        function backToGame() {
            location.reload();
        }

        function deposit() {
            const amount = parseFloat(prompt('Enter deposit amount (ETB):'));
            if (!amount || amount <= 0) return;
            socket.emit('deposit', {user_id: userId, amount: amount});
        }

        renderGrid();
        document.getElementById('roundId').innerText = `ID: ${roundId}`;
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
