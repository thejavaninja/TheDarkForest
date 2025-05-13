import eventlet, math, random
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit

eventlet.monkey_patch()

# ──────────────────────────────────────────────────────────
# CONSTANTS
# ──────────────────────────────────────────────────────────
BOARD        = 101
NUM_SHIPS    = 3

LASER_RADIUS = 5
BOMB_RADIUS  = 2
BOMB_RANGE   = 8          # bomb must be ≤ 8 from a friendly ship
SHOT_LIMIT   = 3

HIT_CREDIT   = 100        # credits per laser hit on enemy ship

# terrain rewards
GAS_CR  = 50
STAR_CR = 100
GEM_BU  = 10
SGEM_BU = 20

BEACON_COST   = 25        # default
BEACON_SALE   = 15        # if any ship sits on a shop

# directions: 0 = ↘ ( +x, +y ), 1 = ↗, 2 = ↖, 3 = ↙
DIR_VECT = [(1,1),(1,-1),(-1,-1),(-1,1)]

# FIXED TERRAIN MAP (Random-Like Positions without Randomness)
# ──────────────────────────────────────────────────────────
def oval(cx, cy, rx, ry):
    """ Generate a fixed oval that may partially spill off the board """
    for y in range(cy - ry, cy + ry + 1):
        for x in range(cx - rx, cx + rx + 1):
            if ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 < 1:
                yield y * BOARD + x

gas, star, gem, sgem, shop = set(), set(), set(), set(), set()

# Gas Ovals: 10 ovals, radius ≈ 6 (fixed positions and sizes)
gas.update(oval(35, 25, 6, 5))
gas.update(oval(65, 22, 7, 6))
gas.update(oval(52, 12, 6, 6))
gas.update(oval(28, 55, 7, 5))
gas.update(oval(70, 52, 6, 6))
gas.update(oval(53, 33, 5, 6))
gas.update(oval(14, 77, 7, 5))
gas.update(oval(92, 78, 6, 7))
gas.update(oval(20, 68, 5, 6))
gas.update(oval(84, 72, 7, 6))
gas.update(oval(58, 88, 6, 5))

# Star Ovals: 8 ovals, radius ≈ 4 (fixed positions and sizes)
star.update(oval(30, 30, 4, 4))
star.update(oval(75, 28, 5, 4))
star.update(oval(50, 24, 4, 5))
star.update(oval(38, 58, 4, 4))
star.update(oval(63, 53, 5, 4))
star.update(oval(48, 42, 4, 4))
star.update(oval(23, 82, 4, 5))
star.update(oval(79, 86, 4, 4))

# Gem Ovals: 14 ovals, radius ≈ 5 (fixed positions and sizes)
gem.update(oval(32, 15, 5, 4))
gem.update(oval(68, 12, 6, 5))
gem.update(oval(17, 38, 4, 5))
gem.update(oval(82, 37, 5, 4))
gem.update(oval(27, 63, 5, 4))
gem.update(oval(73, 66, 4, 6))
gem.update(oval(52, 64, 5, 5))
gem.update(oval(39, 46, 4, 5))
gem.update(oval(61, 44, 5, 4))
gem.update(oval(50, 78, 4, 5))
gem.update(oval(12, 88, 5, 4))
gem.update(oval(88, 91, 4, 4))
gem.update(oval(15, 52, 6, 5))
gem.update(oval(92, 47, 4, 5))

# Super Gem Ovals: 8 ovals, radius ≈ 3 (fixed positions and sizes)
sgem.update(oval(22, 17, 3, 3))
sgem.update(oval(78, 18, 3, 4))
sgem.update(oval(18, 44, 4, 3))
sgem.update(oval(86, 48, 3, 4))
sgem.update(oval(33, 68, 4, 3))
sgem.update(oval(63, 71, 3, 4))
sgem.update(oval(28, 87, 4, 3))
sgem.update(oval(72, 85, 3, 4))

# Shops - Keep them as defined rectangular clusters
for cx, cy in [(5, 5), (95, 95), (5, 95), (95, 5), (50, 50),
               (25, 25), (75, 75), (25, 75), (75, 25), (50, 10)]:
    for y in range(cy, cy + 3):
        for x in range(cx, cx + 3):
            shop.add(y * BOARD + x)

# Priority: shop > sgem > gem > star > gas
gas -= star | gem | sgem | shop
star -= gem | sgem | shop
gem -= sgem | shop
sgem -= shop

# ──────────────────────────────────────────────────────────
# GAME DATA STRUCTURES
# ──────────────────────────────────────────────────────────
def start_pos():
    c = BOARD//2
    return [c*BOARD+c, c*BOARD+(c-1), (c-1)*BOARD+c]

game = {p: {'sid': None,
            'ships': start_pos(),
            'old':   [],
            'credits': 0,
            'bucks':   0,
            'base':    None,
            # ← NEW: stacking upgrades
            'upg': {'laser_spread': 0,
                    'laser_shots' : 0,
                    'plunder'     : 0,
                    'gasman'      : 0}
           }
        for p in ('X', 'O')}


player_phase   = {'X':'base', 'O':'base'}   # 'base'|'fire'|'move'|'done'
moves_pending  = {'X': None, 'O': None}
players        = {}                         # sid ➜ role
beacons        = []                         # list of dicts {owner,cell,dir,hit}
game_over      = False

# ──────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────
def dist_cells(a,b):
    
    ar,ac = divmod(a,BOARD)
    br,bc = divmod(b,BOARD)
    return math.hypot(ar-br, ac-bc)
def discount(role):
    """$10 off if any ship *started* the move on a shop tile."""
    return 10 if any(s in shop for s in game[role]['old']) else 0

def pub_state():
    return {
        'phase'  : player_phase,
        'credits': {p: game[p]['credits'] for p in ('X','O')},
        'bucks'  : {p: game[p]['bucks']   for p in ('X','O')}
    }

def send_state():
    socketio.emit('state', pub_state())

# ──────────────────────────────────────────────────────────
# FLASK / SOCKET.IO APP
# ──────────────────────────────────────────────────────────
app = Flask(__name__)
socketio = SocketIO(app)

@app.route('/')
def index():
    return render_template('index.html')

# ── CONNECT / DISCONNECT ────────────────────────────────
@socketio.on('connect')
def on_connect():
    sid  = request.sid
    if game_over:
        emit('message','Match finished – wait for reset.')
        return

    role = 'X' if game['X']['sid'] is None else ('O' if game['O']['sid'] is None else None)
    if role is None:
        emit('message','Game full.')
        return

    players[sid] = role
    game[role]['sid'] = sid
    player_phase[role] = 'fire' if game[role]['base'] else 'base'

    emit('role',{
        'role'    : role,
        'ships'   : game[role]['ships'],
        'credits' : game[role]['credits'],
        'bucks'   : game[role]['bucks'],
        'needBase': game[role]['base'] is None,
        'map': {'gas':list(gas),'star':list(star),'gem':list(gem),
                'sgem':list(sgem),'shop':list(shop)},
        'beacons' : beacons       # send all existing beacons
    })
    send_state()

@socketio.on('disconnect')
def on_disconnect():
    sid = request.sid
    role = players.pop(sid,None)
    if role:
        game[role]['sid'] = None
        send_state()

# ── BASE PLACEMENT ──────────────────────────────────────
@socketio.on('set_base')
def set_base(data):
    role = players.get(request.sid)
    if role and player_phase[role]=='base':
        cell = data.get('cell')
        if isinstance(cell,int) and 0 <= cell < BOARD*BOARD:
            game[role]['base'] = cell
            player_phase[role] = 'fire'
            emit('base_confirmed')
            send_state()

# ── FIRING PHASE ────────────────────────────────────────
@socketio.on('fire_confirm')
def fire_confirm(data):
    global game_over
    role = players.get(request.sid)
    if game_over or role is None or player_phase[role] != 'fire':
        return

    opp = 'O' if role=='X' else 'X'
    upg  = game[role]['upg']
    cap  = SHOT_LIMIT   + upg['laser_shots']
    rad  = LASER_RADIUS + upg['laser_spread']

    shots = data.get('shots', [])[:cap]
    if len(shots) < cap:

        emit('message', f'Place {cap} shots before confirming.'); return

    for s in shots:
        cell  = s.get('cell')
        wtype = s.get('type')
        if not isinstance(cell,int) or wtype not in ('laser','bomb'): continue

        # legality for bomb
        if wtype=='bomb' and all(dist_cells(cell,sh) > BOMB_RANGE for sh in game[role]['ships']):
            continue  # illegal bomb -> ignore shot

        # send visual feedback
        emit('result',{'cell':cell,'hit':False,'type':wtype},room=game[role]['sid'])
        if game[opp]['sid']:
            emit('opponent_guess',{'cell':cell,'type':wtype},room=game[opp]['sid'])

        # evaluate hit
        if wtype=='laser':
            for sh in game[opp]['ships']:
                if dist_cells(cell,sh) <= rad:
                    game[role]['credits'] += HIT_CREDIT
                    emit('result',{'cell':cell,'hit':True,'type':wtype},room=game[role]['sid'])
                    break
        else:  # bomb
            base = game[opp]['base']
            if base and dist_cells(cell,base) <= BOMB_RADIUS:
                whistle_victory(role,opp); return

    # advance to move phase
    game[role]['old']  = game[role]['ships'][:]
    player_phase[role] = 'move'
    emit('start_move',{'old':game[role]['old']})
    send_state()

# ── LIVE MOVE PACKETS ───────────────────────────────────
@socketio.on('move_packet')
def move_packet(data):
    """
    data = {'ships':[ids], 'radii':[floats]}
    Sends a live preview of move‑cost and resource pickup to the mover only.
    """
    role = players.get(request.sid)
    if role is None or player_phase[role] != 'move':
        return

    moves_pending[role] = data
    ships, radii = data['ships'], data['radii']

    # cost (server uses ceil(sum(radius_final)))
    cost = math.ceil(sum(radii))

    # resource gains (no more shop conversion)
    fuel_gain  = sum(STAR_CR if s in star else GAS_CR if s in gas else 0 for s in ships)
    bucks_gain = sum(SGEM_BU if s in sgem else GEM_BU if s in gem else 0 for s in ships)

    emit('move_preview', {
        'cost' : cost,
        'fuel' : fuel_gain,
        'bucks': bucks_gain
    }, room=request.sid)

    
# ── SHOP PURCHASE (Beacon / Upgrades) ───────────────────────────
@socketio.on('shop_item')
def shop_item(data):
    role = players.get(request.sid)
    if role is None or player_phase[role] != 'move':
        return

    key = data.get('item')               # beacon | laser_spread | laser_shots | plunder | gasman
    PRICES = dict(beacon=25,
                  laser_spread=30,
                  laser_shots =35,
                  plunder     =30,
                  gasman      =30)

    if key not in PRICES:                # silent ignore
        return

    cost = PRICES[key] - discount(role)
    if game[role]['bucks'] < cost:
        return

    game[role]['bucks'] -= cost

    if key == 'beacon':
        #game[role]['bucks'] -= cost  # Deduct bucks immediately
        emit('beacon_ready', room=request.sid)
           # client will start drag
    else:
        game[role]['upg'][key] += 1                       # stack upgrade
        send_state()                                      # update Buck balance for both clients


# ── CONFIRM BEACON ───────────────────────────────────────
@socketio.on('confirm_beacon')
def confirm_beacon(data):
    role = players.get(request.sid)
    if role is None or player_phase[role] != 'move':
        return

    cell = data.get('cell'); dir = data.get('dir')
    if not isinstance(cell,int) or dir not in (0,1,2,3):
        return

    # check if opponent base lies in rectangle
    opp = 'O' if role=='X' else 'X'
    bx = by = None
    if game[opp]['base'] is not None:
        bx,by = divmod(game[opp]['base'],BOARD)[::-1]

    x0,y0 = divmod(cell,BOARD)[::-1]
    dx,dy = DIR_VECT[dir]
    # dir 0 (↘) and 3 (↙) extend to the RIGHT edge; 1,2 go LEFT.
    x1 = 100 if dir in (0, 3) else 0
    # dir 0 (↘) and 1 (↗) extend to the BOTTOM edge; 2,3 go TOP.
    y1 = 100 if dir in (0, 1) else 0

    hit = False
    if bx is not None:
        if min(x0,x1) <= bx <= max(x0,x1) and min(y0,y1) <= by <= max(y0,y1):
            hit = True

    beacon = {'owner':role,'cell':cell,'dir':dir,'hit':hit}
    beacons.append(beacon)

    # share with everyone (opponent does NOT know hit result)
    socketio.emit('new_beacon',{'owner':role,'cell':cell,'dir':dir,'hit':hit})

# ── END TURN ─────────────────────────────────────────────
@socketio.on('end_turn')
def end_turn():
    role = players.get(request.sid)
    if role is None or player_phase[role] != 'move':
        return

    # ⬇︎ NEW: if the player never sent any move_packet, assume “no‑move”
    if moves_pending[role] is None:
        moves_pending[role] = {
            'ships': game[role]['old'][:],   # stay where they were
            'radii': [0, 0, 0]               # zero cost circles
        }

    player_phase[role] = 'done'
    emit('waiting')
    resolve_if_both_done()

def resolve_if_both_done():
    if player_phase['X']!='done' or player_phase['O']!='done':
        send_state(); return

    gains = {'X':{'fuel':0,'bucks':0}, 'O':{'fuel':0,'bucks':0}}

    # apply moves, figure totals
    for role in ('X','O'):
        mp     = moves_pending[role]
        ships  = mp['ships']; radii = mp['radii']
        fixed  = [max(r, dist_cells(o,n)) for o,n,r in zip(game[role]['old'],ships,radii)]

        upg = game[role]['upg']

        fuel  = sum((STAR_CR + upg['gasman']*50) if s in star
            else (GAS_CR + upg['gasman']*25) if s in gas else 0
            for s in ships)

        bucks = sum((SGEM_BU + upg['plunder']*10) if s in sgem
            else (GEM_BU + upg['plunder']*5) if s in gem else 0
            for s in ships)
        gains[role]['fuel']=fuel
        gains[role]['bucks']=bucks

        game[role]['ships']   = ships
        game[role]['credits'] += fuel - math.ceil(sum(fixed))
        game[role]['bucks']  += bucks

    # send enemy intel markers *and* new enemy_totals packet
    for viewer, enemy in (('X','O'),('O','X')):
        sid = game[viewer]['sid']
        if not sid: continue
        ep = moves_pending[enemy]
        emit('enemy_markers',{
            'old': game[enemy]['old'],
            'rad': ep['radii'],
            'cr' : [STAR_CR if s in star else GAS_CR if s in gas else 0 for s in ep['ships']],
            'bk' : [SGEM_BU if s in sgem else GEM_BU if s in gem else 0 for s in ep['ships']]
        }, room=sid)
        emit('enemy_totals', gains[enemy], room=sid)

    # prepare next turn
    moves_pending['X']=moves_pending['O']=None
    player_phase['X']=player_phase['O']='fire'
    send_state()

# ── RESET ────────────────────────────────────────────────
@socketio.on('reset_request')
def reset_request():
    global game_over, beacons
    game_over = False
    beacons   = []
    for p in ('X','O'):
        game[p]['ships']   = start_pos()
        game[p]['old']     = []
        game[p]['credits'] = 0
        game[p]['bucks']   = 0
        game[p]['base']    = None
        moves_pending[p]   = None
        player_phase[p]    = 'base'
    socketio.emit('reset')
    send_state()

# ── VICTORY ──────────────────────────────────────────────
def whistle_victory(winner,loser):
    global game_over
    game_over=True
    if game[winner]['sid']:
        emit('game_over',{'result':'victory'},room=game[winner]['sid'])
    if game[loser]['sid']:
        emit('game_over',{'result':'defeat'},room=game[loser]['sid'])

# ──────────────────────────────────────────────────────────
if __name__ == '__main__':
    socketio.run(app,host='0.0.0.0',port=5000,debug=True)
