import os
import sys
import time
import requests
import RPi.GPIO as GPIO
from time import sleep
from mfrc522 import SimpleMFRC522
import dht11
import I2C_LCD_driver

os.chdir(os.path.dirname(os.path.abspath(__file__))) #make sure credentials.csv is found regardless of how this got launched (terminal cd, IDLE double-click, IDLE Open dialog, etc.)

GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)

#---the real cold-chain limits this locker is designed around---
#8 deg C is the upper limit for refrigerated pharmaceuticals, and 60 %RH is the
#storage guideline from WHO Technical Report Series 961 Annex 5 (also supported by
#USP <797> and the ISPE/ASHRAE 30-60 %RH range). These are the values used in Live
#mode. Demo mode lets the operator enter their own limits instead, so that all
#three alert levels can be shown in an ordinary room that is not refrigerated.
LIVE_TEMP_THRESHOLD = 8
LIVE_HUMIDITY_THRESHOLD = 60

#---cloud upload (ThingSpeak) and notifications (Telegram)---
THINGSPEAK_WRITE_API_KEY = "Q52UWKYRJ9V66566"
TELEGRAM_TOKEN = "8820533235:AAE97uZmsaVa0dX3bGLV21wTbMFh0t6ymoo"
TELEGRAM_CHAT_ID = "7995800244"

#Both of the functions below talk to the internet, so both are given a 5 second timeout
#and are wrapped so that a network problem cannot take the locker down with it. The door,
#keypad, sensors and breach logic are safety functions and have to keep working whether or
#not there is WiFi - losing the cloud link should only cost us the logging and the alerts.
NETWORK_TIMEOUT = 5 #seconds to wait before giving up on a cloud request

def upload_to_thingspeak(temperature, humidity, tilt_status):
    #field1 = temperature, field2 = humidity, field3 = tilt/breach status
    url = "https://api.thingspeak.com/update"
    payload = {
        "api_key": THINGSPEAK_WRITE_API_KEY,
        "field1": temperature,
        "field2": humidity,
        "field3": tilt_status
    }
    try:
        requests.get(url, params=payload, timeout=NETWORK_TIMEOUT)
    except Exception:
        print("ThingSpeak upload failed - no network? Locker still running.")

def send_telegram_alert(message):
    url = "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message
    }
    try:
        requests.get(url, params=payload, timeout=NETWORK_TIMEOUT)
        return True
    except Exception:
        print("Telegram alert failed - no network? Locker still running.")
        return False

def describe_conditions(temperature, humidity, tilt_state, temp_bad, humidity_bad, tilt_bad, TEMP_THRESHOLD, HUMIDITY_THRESHOLD):
    #builds a line-by-line breakdown of exactly which factor(s) are out of range and by
    #how much, so an alert read on a phone during transport says what is actually wrong
    #without the reader needing to know the thresholds or do any comparison themselves
    lines = []

    if temperature is None:
        lines.append("Temperature: sensor reading unavailable")
    elif temp_bad:
        over_by = temperature - TEMP_THRESHOLD
        lines.append("Temperature: " + str(temperature) + " deg C (limit " + str(TEMP_THRESHOLD) + " deg C) - OVER by " + str(round(over_by, 1)) + " deg C")
    else:
        lines.append("Temperature: " + str(temperature) + " deg C (limit " + str(TEMP_THRESHOLD) + " deg C) - OK")

    if humidity is None:
        lines.append("Humidity: sensor reading unavailable")
    elif humidity_bad:
        over_by = humidity - HUMIDITY_THRESHOLD
        lines.append("Humidity: " + str(humidity) + " %RH (limit " + str(HUMIDITY_THRESHOLD) + " %RH) - OVER by " + str(round(over_by, 1)) + " %RH")
    else:
        lines.append("Humidity: " + str(humidity) + " %RH (limit " + str(HUMIDITY_THRESHOLD) + " %RH) - OK")

    if tilt_bad:
        lines.append("Tilt/tamper switch: TRIGGERED - the locker has been moved or tilted")
    else:
        lines.append("Tilt/tamper switch: OK - resting normally")

    return "\n".join(lines)

#---pin setup (from the shield's own pin map, see RPi Training Kit User Guide)---
TILT_PIN = 22       #confirmed by pin-scan: the tilt switch is wired to the shield's slide switch terminal. LOW = normal, HIGH = tilt/breach
STATUS_LED_PIN = 24  #the shield only has ONE built-in LED (confirmed red) - solid on = approved/unlocked, blinking = denied/breach
BUZZER_PIN = 18      #shield's built-in buzzer
SERVO_PIN = 26       #shield's built-in servo, used as the deadbolt actuator
DHT11_PIN = 21       #shield's built-in DHT11

GPIO.setup(TILT_PIN, GPIO.IN, pull_up_down=GPIO.PUD_DOWN) #pull-down needed for a clean, non-floating reading on this pin
GPIO.setup(STATUS_LED_PIN, GPIO.OUT)
GPIO.setup(BUZZER_PIN, GPIO.OUT)
GPIO.setup(SERVO_PIN, GPIO.OUT)

servo = GPIO.PWM(SERVO_PIN, 50) #50Hz, standard servo frequency
servo.start(0)

#duty cycle for locked (0 deg) and unlocked (90 deg) positions
#found by trial & error, same approach taught for the servo motor sample - adjust these two numbers if your servo doesn't fully lock/unlock
LOCK_DUTY = 3
UNLOCK_DUTY = 8

def lock_servo():
    servo.ChangeDutyCycle(LOCK_DUTY)
    sleep(0.5)
    servo.ChangeDutyCycle(0) #stop sending pulses so the servo doesn't jitter while idle

def unlock_servo():
    servo.ChangeDutyCycle(UNLOCK_DUTY)
    sleep(0.5)
    servo.ChangeDutyCycle(0)

lock_servo() #make sure the vault starts locked

#---4x3 keypad setup, same wiring as the shield's "11. keypad.py" sample---
MATRIX = [[1,2,3],
          [4,5,6],
          [7,8,9],
          ['*',0,'#']]
ROW = [6,20,19,13]
COL = [12,5,16]

for i in range(3):
    GPIO.setup(COL[i], GPIO.OUT)
    GPIO.output(COL[i], 1)
for j in range(4):
    GPIO.setup(ROW[j], GPIO.IN, pull_up_down=GPIO.PUD_UP)

def read_keypad_digit():
    for i in range(3):
        GPIO.output(COL[i], 0)
        for j in range(4):
            if GPIO.input(ROW[j]) == 0:
                key = MATRIX[j][i]
                while GPIO.input(ROW[j]) == 0: #debounce
                    sleep(0.1)
                GPIO.output(COL[i], 1)
                return key
        GPIO.output(COL[i], 1)
    return None

def read_pin_from_keypad():
    #collects digits until # is pressed, * clears what's been typed so far
    #shows one "*" per digit on the LCD's second line instead of the real digits
    pin = ""
    while True:
        key = read_keypad_digit()
        if key is not None:
            if key == '#':
                break
            elif key == '*':
                pin = ""
                lcd.lcd_display_string("                ", 2) #16 spaces, clears the line
            else:
                pin = pin + str(key)
                stars = "*" * len(pin)
                lcd.lcd_display_string(stars.ljust(16), 2)
        sleep(0.05)
    return pin

#---LCD, RFID, DHT11 setup---
lcd = I2C_LCD_driver.lcd()
reader = SimpleMFRC522()
dht_sensor = dht11.DHT11(pin=DHT11_PIN)

def show_lcd(line1, line2=""):
    lcd.lcd_clear()
    lcd.lcd_display_string(line1, 1)
    if line2:
        lcd.lcd_display_string(line2, 2)

def blink_led(pin, times=3):
    for i in range(times):
        GPIO.output(pin, 1)
        sleep(0.2)
        GPIO.output(pin, 0)
        sleep(0.2)

def sound_buzzer(duration=0.3):
    GPIO.output(BUZZER_PIN, 1)
    sleep(duration)
    GPIO.output(BUZZER_PIN, 0)

def read_single_key():
    #waits for one key press on the keypad and returns it
    key = None
    while key is None:
        key = read_keypad_digit()
        sleep(0.05)
    return key

def read_number_from_keypad(prompt):
    #types a whole number on the keypad instead of a computer keyboard, so the locker
    #can be set up on the device itself. * clears what has been typed, # confirms.
    #The digits appear on the LCD's second line as they are pressed.
    digits = ""
    show_lcd(prompt, "")
    while True:
        key = read_keypad_digit()
        if key is not None:
            if key == '#':
                if digits != "":
                    break
            elif key == '*':
                digits = ""
                lcd.lcd_display_string("                ", 2) #16 spaces, clears the line
            else:
                digits = digits + str(key)
                lcd.lcd_display_string(digits.ljust(16), 2)
        sleep(0.05)
    print(prompt + " " + digits)
    return int(digits)

def check_credentials(uid, pin):
    #credentials.csv holds one "uid,pin" pair per line, built by register_card.py
    f = open("credentials.csv", "r")
    lines = f.readlines()
    f.close()
    for line in lines:
        line = line.strip()
        if line == "":
            continue
        parts = line.split(",")
        if len(parts) < 2:
            continue #skip a damaged line rather than letting it stop every card read
        stored_uid = parts[0]
        stored_pin = parts[1]
        if stored_uid == uid:
            if stored_pin == pin:
                return "OK"
            else:
                return "WRONG_PIN"
    return "UNKNOWN_CARD"

#=====================================================================
# CARD REGISTRATION MODE - run this once to add a new card, then run
# the program normally. Two ways to use it:
#   python3 main.py register 123   <- PIN typed on the computer keyboard
#   python3 main.py register       <- PIN typed on the shield's keypad
#=====================================================================
if len(sys.argv) > 1 and sys.argv[1] == "register":
    show_lcd("Register card", "Tap new card...")
    print("Registration mode - hold the new card near the reader")
    new_uid = str(reader.read_id())
    print("Card UID:", new_uid)

    if len(sys.argv) > 2:
        new_pin = sys.argv[2]
        print("Using PIN from the command line:", new_pin)
    else:
        show_lcd("Enter new PIN", "then press #")
        new_pin = read_pin_from_keypad()

    f = open("credentials.csv", "a+")
    f.write(new_uid + "," + new_pin + "\n")
    f.close()

    print("Registered UID", new_uid, "with PIN", new_pin)
    show_lcd("Card registered", "Run again to use")
    sleep(3)
    servo.stop()
    GPIO.cleanup()
    sys.exit()

#---state variables---
breach_flag = False       #HARD lockout - all 3 of temp/humidity/tilt are bad AT THE SAME TIME. Vault will not open even with the correct PIN.
warning_active = False    #SOFT warning - only 1 or 2 of the 3 are bad. Vault still opens, but shows a warning + 2s delay first.
last_temperature = None
last_humidity = None
last_tilt_state = 0
vault_is_open = False #True only during the 10-second window after a successful unlock
last_upload_time = 0
UPLOAD_INTERVAL = 20 #seconds, matches the 20s ThingSpeak rate used in Lesson 5
last_dht_read_time = 0
DHT_READ_INTERVAL = 2 #seconds - the DHT11 can't be read reliably faster than about once a second
#TEMP_THRESHOLD and HUMIDITY_THRESHOLD are chosen by the startup menu further down

def poll_sensors_and_check_breach():
    #reads DHT11 + tilt switch, updates the global breach_flag/warning_active, uploads to
    #ThingSpeak every 20s, and immediately alerts Telegram the moment either state starts -
    #including whether the vault happened to be open or locked at that exact moment, since a
    #breach while open (medicine physically accessible) is more urgent than while locked
    global breach_flag, warning_active, last_upload_time, last_temperature, last_humidity, last_tilt_state, last_dht_read_time

    tilt_state = GPIO.input(TILT_PIN) #HIGH = breach, LOW = normal

    #only read the DHT11 once every DHT_READ_INTERVAL - polling it every loop (~0.1s) is faster
    #than the sensor can respond, so most reads come back invalid and were wiping out a real
    #reading with None every other loop, making the breach/warning flags flap on and off rapidly
    #instead of only when conditions actually changed. A failed read now just keeps the last
    #known good values instead of blanking them.
    now_for_dht = time.time()
    if (now_for_dht - last_dht_read_time) >= DHT_READ_INTERVAL:
        result = dht_sensor.read()
        last_dht_read_time = now_for_dht
        if result.is_valid():
            last_temperature = result.temperature
            last_humidity = result.humidity

    temperature = last_temperature
    humidity = last_humidity
    last_tilt_state = tilt_state

    temp_bad = temperature is not None and temperature > TEMP_THRESHOLD
    humidity_bad = humidity is not None and humidity > HUMIDITY_THRESHOLD
    tilt_bad = tilt_state == 1

    is_hard_breach = temp_bad and humidity_bad and tilt_bad #ALL three at once
    is_soft_warning = (temp_bad or humidity_bad or tilt_bad) and not is_hard_breach #1 or 2, but not all 3

    if is_hard_breach and not breach_flag:
        breach_flag = True
        vault_state_text = "UNLOCKED - vault is currently open!" if vault_is_open else "locked"
        details = describe_conditions(temperature, humidity, tilt_state, temp_bad, humidity_bad, tilt_bad, TEMP_THRESHOLD, HUMIDITY_THRESHOLD)
        send_telegram_alert("ALERT: HARD BREACH\nSmart Medical Locker locked down at " + time.strftime("%Y-%m-%d %H:%M:%S") + "\nVault status: " + vault_state_text + "\n\n" + details + "\n\nAccess is disabled until all conditions return to normal.")
        blink_led(STATUS_LED_PIN, 3)

    if (not is_hard_breach) and breach_flag:
        #conditions back to normal - clear the flag so the vault can be used again
        breach_flag = False

    if is_soft_warning and not warning_active:
        warning_active = True
        details = describe_conditions(temperature, humidity, tilt_state, temp_bad, humidity_bad, tilt_bad, TEMP_THRESHOLD, HUMIDITY_THRESHOLD)
        send_telegram_alert("WARNING: anomaly detected\nSmart Medical Locker at " + time.strftime("%Y-%m-%d %H:%M:%S") + "\n\n" + details + "\n\nAccess is still allowed but this event has been flagged for review.")

    if (not is_soft_warning) and warning_active:
        warning_active = False

    #non-blocking cloud telemetry - only upload every 20 seconds
    current_time = time.time()
    if (current_time - last_upload_time) >= UPLOAD_INTERVAL:
        if temperature is not None:
            upload_to_thingspeak(temperature, humidity, tilt_state)
        last_upload_time = current_time

#=====================================================================
# MODE MENU - choose the limits on the locker itself, using the keypad
# and the LCD. No computer keyboard is needed.
#
#   1 = Live mode  : the real cold-chain limits this locker is built for
#   2 = Demo mode  : type your own limits, so all three alert levels can
#                    be shown in an ordinary room that is not a fridge
#
# Runs once at startup, and again any time * is pressed while the vault
# is idle (waiting for a card) - so switching modes never needs going
# back to the terminal. The command-line shortcuts only apply on that
# first startup call, not on a later switch from the keypad:
#   python3 main.py live
#   python3 main.py demo 27 70      <- temperature limit, humidity limit
#=====================================================================
def choose_mode(is_startup):
    global TEMP_THRESHOLD, HUMIDITY_THRESHOLD

    print("")
    print("=========================================")
    print("        SMART MEDICAL LOCKER")
    print("=========================================")

    if is_startup and len(sys.argv) > 1 and sys.argv[1] == "live":
        choice = "1"
    elif is_startup and len(sys.argv) > 3 and sys.argv[1] == "demo":
        choice = "2"
    else:
        print("  Choose on the keypad:")
        print("    1 = Live mode (" + str(LIVE_TEMP_THRESHOLD) + " deg C, " + str(LIVE_HUMIDITY_THRESHOLD) + " %RH)")
        print("    2 = Demo mode (set your own limits)")
        show_lcd("1=Live  2=Demo", "Press a key...")
        choice = ""
        while choice != "1" and choice != "2":
            choice = str(read_single_key())

    if choice == "1":
        TEMP_THRESHOLD = LIVE_TEMP_THRESHOLD
        HUMIDITY_THRESHOLD = LIVE_HUMIDITY_THRESHOLD
        show_lcd("Live mode", "T" + str(TEMP_THRESHOLD) + "C  H" + str(HUMIDITY_THRESHOLD) + "%")
        print("Live mode - temperature limit " + str(TEMP_THRESHOLD) + " deg C, humidity limit " + str(HUMIDITY_THRESHOLD) + " %RH")
        sleep(2)

    else:
        #show what the sensors are reading right now, before any limits are chosen, so the
        #operator can set them either side of the real room conditions instead of guessing.
        #The same reading is pushed to Telegram so it can be read off a phone as well.
        show_lcd("Demo mode", "Reading sensor")
        print("Reading the sensors, please wait...")

        setup_temp = None
        setup_humidity = None
        for i in range(10):
            setup_result = dht_sensor.read()
            if setup_result.is_valid():
                setup_temp = setup_result.temperature
                setup_humidity = setup_result.humidity
                break
            sleep(1.5)

        setup_tilt = GPIO.input(TILT_PIN)

        reading_text = "temperature " + str(setup_temp) + " deg C, humidity " + str(setup_humidity) + " %RH, tilt " + str(setup_tilt)
        print("Right now the locker reads: " + reading_text)
        print("Set a limit BELOW a reading to make it count as bad, or ABOVE it to keep it good.")
        sent = send_telegram_alert("Smart Medical Locker - demo setup. The locker currently reads " + reading_text + ". Choose the temperature and humidity limits either side of these values.")
        if sent:
            print("(the same reading has been sent to Telegram)")
            lcd_line2 = "Sent to Telegram"
        else:
            lcd_line2 = "No network"

        #hold the live readings on the screen so they can be copied down before typing limits
        show_lcd("Now T" + str(setup_temp) + " H" + str(setup_humidity), lcd_line2)
        sleep(5)

        if is_startup and len(sys.argv) > 3:
            TEMP_THRESHOLD = int(sys.argv[2])
            HUMIDITY_THRESHOLD = int(sys.argv[3])
        else:
            TEMP_THRESHOLD = read_number_from_keypad("Temp limit? #=ok")
            HUMIDITY_THRESHOLD = read_number_from_keypad("Humid limit?#=ok")

        show_lcd("Demo mode", "T" + str(TEMP_THRESHOLD) + "C  H" + str(HUMIDITY_THRESHOLD) + "%")
        print("Demo mode - temperature limit " + str(TEMP_THRESHOLD) + " deg C, humidity limit " + str(HUMIDITY_THRESHOLD) + " %RH")
        sleep(2)

    print("Smart Medical Locker ready.")
    show_lcd("Vault Secured", "Tap card...")

choose_mode(is_startup=True)

while True:

    #1. read sensors and check for a breach at the top of every loop
    poll_sensors_and_check_breach()

    #2. non-blocking RFID scan
    uid = reader.read_id_no_block()

    if uid is None:
        #while idle (no card present), * reopens the mode menu so Live/Demo
        #can be switched, or the demo limits changed, without touching the
        #terminal at all
        key = read_keypad_digit()
        if key == '*':
            choose_mode(is_startup=False)
        sleep(0.1) #100ms idle delay, then loop back up to keep polling sensors
        continue

    uid = str(uid)

    #3. RFID + PIN authentication sequence
    show_lcd("Card detected", "Enter PIN:")
    pin = read_pin_from_keypad()

    auth_result = check_credentials(uid, pin)

    if auth_result == "UNKNOWN_CARD":
        show_lcd("Access Denied", "Unknown card")
        send_telegram_alert("Access denied: unknown card UID " + uid)
        blink_led(STATUS_LED_PIN, 3)
        sound_buzzer()
        sleep(1.5) #let the denial message stay readable before resetting
        show_lcd("Vault Secured", "Tap card...")

    elif auth_result == "WRONG_PIN":
        show_lcd("Access Denied", "Wrong PIN")
        send_telegram_alert("Access denied: wrong PIN for card UID " + uid)
        blink_led(STATUS_LED_PIN, 3)
        sound_buzzer()
        sleep(1.5) #let the denial message stay readable before resetting
        show_lcd("Vault Secured", "Tap card...")

    else: #auth_result == "OK"
        if breach_flag:
            #environmental escrow gate - correct card+PIN, but vault stays locked
            show_lcd("VAULT LOCKED", "Breach detected")
            send_telegram_alert("Access blocked: valid card " + uid + " but a breach is active")
            blink_led(STATUS_LED_PIN, 5)
            sound_buzzer(1)
            sleep(1.5) #let the breach message stay readable before resetting
            show_lcd("Vault Secured", "Tap card...")

        else:
            if warning_active:
                #soft warning - only 1 or 2 of the 3 metrics are bad, not a full breach.
                #show a detailed warning for 2 seconds, then proceed to unlock as normal.
                warning_detail = "T" + str(last_temperature) + " H" + str(last_humidity) + " Ti" + str(last_tilt_state)
                show_lcd("WARNING: check", warning_detail[:16])
                sound_buzzer(0.2)
                sleep(2)

            #all clear (or warning acknowledged) - unlock
            show_lcd("Access Approved", "Unlocking...")
            GPIO.output(STATUS_LED_PIN, 1)
            unlock_servo()
            vault_is_open = True
            send_telegram_alert("Access granted for card UID " + uid)

            open_start_time = time.time()
            while (time.time() - open_start_time) < 10:
                poll_sensors_and_check_breach() #keep watching for a breach WHILE the vault is open, not just before
                key = read_keypad_digit()
                if key == '*':
                    #manual early close - user pressed * instead of waiting out the 10s window
                    break
                sleep(0.1) #10 second open window

            lock_servo()
            vault_is_open = False
            GPIO.output(STATUS_LED_PIN, 0)
            show_lcd("Vault Secured", "Tap card...")

    #wait until the card is lifted off the reader before scanning for the next one.
    #without this, a card left resting on the reader is picked up again straight away and
    #the display jumps back to "Enter PIN" by itself, which looks like the system has frozen.
    #sensors keep being polled here so a breach is still caught while we wait.
    while reader.read_id_no_block() is not None:
        poll_sensors_and_check_breach()
        sleep(0.2)

    sleep(0.1)
