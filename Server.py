import eventlet
eventlet.monkey_patch()

import random
import time
import sqlite3
from datetime import datetime
from flask import Flask, render_template, request, session
from flask_socketio import SocketIO, emit, join_room, leave_room

app = Flask(__name__)
app.config['SECRET_KEY'] = 'change-me-to-a-random-string'
socketio = SocketIO(app, async_mode='eventlet')

# Database (same as before)
conn = sqlite3.connect("keno_app.db", check_same_thread=False)
conn.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, balance REAL DEFAULT 100.0)")
conn.commit()

# Game state
class Round:
    def __init__(self, rid):
        self.id = rid
        self.start_time = time.time()
        self.bets = {}          # user_id -> list of tickets
        self.drawn = None
        self.timer = 60         # seconds

current_round = Round(1)

def process_draw(round_obj):
    drawn = sorted(random.sample(range(1, 81), 20))
    round_obj.drawn = drawn
    paytable = {0:0,1:0,2:0,3:1,4:2,5:5,6:25,7:100,8:500,9:1000,10:5000}
    winners = {}
    for uid, tickets in round_obj.bets.items():
        total_win = 0
        for t in tickets:
            matches = len(set(t['numbers']) & set(drawn))
            mult = paytable.get(matches, 0)
            total_win += t['amount'] * mult
        if total_win > 0:
            # Update balance (real code would use a DB lock)
            cur = conn.cursor()
            cur.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (total_win, uid))
            conn.commit()
        winners[uid] = total_win
    return drawn, winners

# Socket.IO events
@socketio.on('connect')
def handle_connect():
    # Join a global round room
    join_room('round')
    # Send current round state
    elapsed = time.time() - current_round.start_time
    remaining = max(0, current_round.timer - elapsed)
    emit('round_state', {'round_id': current_round.id, 'remaining': remaining, 'drawn': current_round.drawn})

@socketio.on('place_bet')
def handle_bet(data):
    user_id = data['user_id']
    numbers = data['numbers']   # list of ints
    amount = float(data['amount'])
    # Check balance, ticket limit, etc. (simplified)
    cur = conn.cursor()
    cur.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    if row is None:
        cur.execute("INSERT INTO users (user_id) VALUES (?)", (user_id,))
        conn.commit()
        balance = 100.0
    else:
        balance = row[0]
    if amount > balance:
        emit('error', {'message': 'Insufficient balance'})
        return
    if len(current_round.bets.get(user_id, [])) >= 5:
        emit('error', {'message': 'Max 5 tickets'})
        return
    # Deduct balance
    cur.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    # Store ticket
    ticket = {'numbers': numbers, 'amount': amount}
    current_round.bets.setdefault(user_id, []).append(ticket)
    # Confirm to user
    emit('bet_success', {'numbers': numbers, 'amount': amount, 'ticket_count': len(current_round.bets[user_id])})

# Round manager thread
def round_loop():
    global current_round
    while True:
        # Wait for round duration
        socketio.sleep(current_round.timer)
        # Draw numbers
        drawn, winners = process_draw(current_round)
        # Broadcast results to all clients
        socketio.emit('draw_result', {
            'round_id': current_round.id,
            'drawn': drawn,
            'winners': winners
        }, room='round')
        # Start new round
        current_round = Round(current_round.id + 1)
        socketio.emit('new_round', {
            'round_id': current_round.id,
            'duration': current_round.timer
        }, room='round')

# Serve Mini App page
@app.route('/')
def index():
    return render_template('keno.html')

if __name__ == '__main__':
    socketio.start_background_task(round_loop)
    socketio.run(app, host='0.0.0.0', port=5000)
