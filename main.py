import os
import sys
import time
import requests
import RPi.GPIO as GPIO
from time import sleep
from mfrc522 import SimpleMFRC522
import dht11
import I2C_LCD_driver

os.chdir(os.path.dirname(os.path.abspath(__file__))) #so the program can always find its own files, no matter how it was started

GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)

#Real cold-chain limits: vaccines must stay between 2-8 deg C (CDC says freezing at
#0 deg C or below can ruin them, not just overheating). 30-60% RH is the normal safe
#humidity range for medicine storage (WHO and pharma industry guidelines). These are
#used in Live mode. Demo mode lets you type your own range to test the alarms safely.
LIVE_TEMP_LOWER = 2
LIVE_TEMP_UPPER = 8
LIVE_HUMIDITY_LOWER = 30
LIVE_HUMIDITY_UPPER = 60

#---cloud upload (ThingSpeak) and notifications (Telegram)---
THINGSPEAK_WRITE_API_KEY = "Q52UWKYRJ9V66566"
TELEGRAM_TOKEN = "8820533235:AAE97uZmsaVa0dX3bGLV21wTbMFh0t6ymoo"
TELEGRAM_CHAT_ID = "7995800244"

#These two functions use the internet. If WiFi is down they must not freeze the whole
#program, since the lock, keypad and sensors still need to work without it.
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

def describe_conditions(temperature, humidity, tilt_state, temp_bad, humidity_bad, tilt_bad, TEMP_LOWER, TEMP_UPPER, HUMIDITY_LOWER, HUMIDITY_UPPER):
    #builds a short report of what is wrong and by how much, so a Telegram message says
    #exactly what happened without anyone needing to check the numbers themselves
    lines = []

    if temperature is None:
        lines.append("Temperature: sensor reading unavailable")
    elif temp_bad:
        if temperature < TEMP_LOWER:
            gap = TEMP_LOWER - temperature
            lines.append("Temperature: " + str(temperature) + " deg C (range " + str(TEMP_LOWER) + "-" + str(TEMP_UPPER) + " deg C) - TOO LOW by " + str(round(gap, 1)) + " deg C")
        else:
            gap = temperature - TEMP_UPPER
            lines.append("Temperature: " + str(temperature) + " deg C (range " + str(TEMP_LOWER) + "-" + str(TEMP_UPPER) + " deg C) - TOO HIGH by " + str(round(gap, 1)) + " deg C")
    else:
        lines.append("Temperature: " + str(temperature) + " deg C (range " + str(TEMP_LOWER) + "-" + str(TEMP_UPPER) + " deg C) - OK")

    if humidity is None:
        lines.append("Humidity: sensor reading unavailable")
    elif humidity_bad:
        if humidity < HUMIDITY_LOWER:
            gap = HUMIDITY_LOWER - humidity
            lines.append("Humidity: " + str(humidity) + " %RH (range " + str(HUMIDITY_LOWER) + "-" + str(HUMIDITY_UPPER) + " %RH) - TOO LOW by " + str(round(gap, 1)) + " %RH")
        else:
            gap = humidity - HUMIDITY_UPPER
            lines.append("Humidity: " + str(humidity) + " %RH (range " + str(HUMIDITY_LOWER) + "-" + str(HUMIDITY_UPPER) + " %RH) - TOO HIGH by " + str(round(gap, 1)) + " %RH")
    else:
        lines.append("Humidity: " + str(humidity) + " %RH (range " + str(HUMIDITY_LOWER) + "-" + str(HUMIDITY_UPPER) + " %RH) - OK")

    if tilt_bad:
        lines.append("Tilt/tamper switch: TRIGGERED - the locker has been moved or tilted")
    else:
        lines.append("Tilt/tamper switch: OK - resting normally")

    return "\n".join(lines)

#---pin setup (see RPi Training Kit User Guide)---
TILT_PIN = 22        #LOW = normal, HIGH = tilted/moved
STATUS_LED_PIN = 24   #on = unlocked, blinking = denied or breach
BUZZER_PIN = 18
SERVO_PIN = 26        #moves the deadbolt
DHT11_PIN = 21

GPIO.setup(TILT_PIN, GPIO.IN, pull_up_down=GPIO.PUD_DOWN) #pull-down stops the reading from floating randomly
GPIO.setup(STATUS_LED_PIN, GPIO.OUT)
GPIO.setup(BUZZER_PIN, GPIO.OUT)
GPIO.setup(SERVO_PIN, GPIO.OUT)

servo = GPIO.PWM(SERVO_PIN, 50) #50Hz, standard servo frequency
servo.start(0)

#duty cycle for locked and unlocked position, found by testing - change these if your servo doesn't move all the way
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

#---4x3 keypad, wired the same way as the course's keypad sample---
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
    #collects digits until # is pressed, * clears and starts over, shows stars instead of the real digits
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
    key = None
    while key is None:
        key = read_keypad_digit()
        sleep(0.05)
    return key

def read_number_from_keypad(prompt):
    #same idea as read_pin_from_keypad, but shows the real digits instead of stars, since this isn't a secret
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
    #credentials.csv holds one "uid,pin" pair per line
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
breach_flag = False       #true when temperature, humidity AND tilt are ALL bad together - vault stays locked even with the right card and PIN
warning_active = False    #true when 1 or 2 of the 3 are bad - vault still opens but shows a warning first
last_temperature = None
last_humidity = None
last_tilt_state = 0
vault_is_open = False #True only during the 10-second window after a successful unlock
last_upload_time = 0
UPLOAD_INTERVAL = 20 #seconds, matches the 20s ThingSpeak rate used in Lesson 5
last_dht_read_time = 0
DHT_READ_INTERVAL = 2 #seconds - the DHT11 can't be read reliably faster than about once a second
#TEMP_LOWER, TEMP_UPPER, HUMIDITY_LOWER, HUMIDITY_UPPER are chosen by the startup menu further down

def poll_sensors_and_check_breach():
    #checks the sensors and tilt switch, updates breach_flag/warning_active, uploads to
    #ThingSpeak, and sends a Telegram alert the moment a warning or breach starts (not
    #every loop) - also says whether the vault was open or locked at that moment
    global breach_flag, warning_active, last_upload_time, last_temperature, last_humidity, last_tilt_state, last_dht_read_time

    tilt_state = GPIO.input(TILT_PIN) #HIGH = breach, LOW = normal

    #only reads the DHT11 once every DHT_READ_INTERVAL seconds - reading it faster than
    #that fails most of the time and used to make the warning/breach flags flicker on
    #and off. A failed read now just reuses the last good reading instead of blanking it.
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

    #bad means outside the good range, either too low or too high, not just too high
    temp_bad = temperature is not None and (temperature < TEMP_LOWER or temperature > TEMP_UPPER)
    humidity_bad = humidity is not None and (humidity < HUMIDITY_LOWER or humidity > HUMIDITY_UPPER)
    tilt_bad = tilt_state == 1

    is_hard_breach = temp_bad and humidity_bad and tilt_bad #ALL three at once
    is_soft_warning = (temp_bad or humidity_bad or tilt_bad) and not is_hard_breach #1 or 2, but not all 3

    if is_hard_breach and not breach_flag:
        breach_flag = True
        vault_state_text = "UNLOCKED - vault is currently open!" if vault_is_open else "locked"
        details = describe_conditions(temperature, humidity, tilt_state, temp_bad, humidity_bad, tilt_bad, TEMP_LOWER, TEMP_UPPER, HUMIDITY_LOWER, HUMIDITY_UPPER)
        send_telegram_alert("ALERT: HARD BREACH\nSmart Medical Locker locked down at " + time.strftime("%Y-%m-%d %H:%M:%S") + "\nVault status: " + vault_state_text + "\n\n" + details + "\n\nAccess is disabled until all conditions return to normal.")
        blink_led(STATUS_LED_PIN, 3)

    if (not is_hard_breach) and breach_flag:
        breach_flag = False #conditions back to normal - clear the flag so the vault can be used again

    if is_soft_warning and not warning_active:
        warning_active = True
        details = describe_conditions(temperature, humidity, tilt_state, temp_bad, humidity_bad, tilt_bad, TEMP_LOWER, TEMP_UPPER, HUMIDITY_LOWER, HUMIDITY_UPPER)
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
# MODE MENU - pick the good range using the keypad and LCD, no laptop needed.
#   1 = Live mode : the real cold-chain range (see the top of this file)
#   2 = Demo mode : type your own range, so all three alerts can be shown
#                   in a normal room that isn't a fridge
# A reading only counts as bad if it's outside the low-high range.
# Runs at startup, and again if * is pressed while idle, so modes can be
# switched without touching the terminal. Command-line shortcuts (only
# work at startup, not from a later * press):
#   python3 main.py live
#   python3 main.py demo 2 8 30 60   (temp low, temp high, humidity low, humidity high)
#=====================================================================
def choose_mode(is_startup):
    global TEMP_LOWER, TEMP_UPPER, HUMIDITY_LOWER, HUMIDITY_UPPER

    print("")
    print("=========================================")
    print("        SMART MEDICAL LOCKER")
    print("=========================================")

    if is_startup and len(sys.argv) > 1 and sys.argv[1] == "live":
        choice = "1"
    elif is_startup and len(sys.argv) > 5 and sys.argv[1] == "demo":
        choice = "2"
    else:
        print("  Choose on the keypad:")
        print("    1 = Live mode (" + str(LIVE_TEMP_LOWER) + "-" + str(LIVE_TEMP_UPPER) + " deg C, " + str(LIVE_HUMIDITY_LOWER) + "-" + str(LIVE_HUMIDITY_UPPER) + " %RH)")
        print("    2 = Demo mode (set your own range)")
        show_lcd("1=Live  2=Demo", "Press a key...")
        choice = ""
        while choice != "1" and choice != "2":
            choice = str(read_single_key())

    if choice == "1":
        TEMP_LOWER = LIVE_TEMP_LOWER
        TEMP_UPPER = LIVE_TEMP_UPPER
        HUMIDITY_LOWER = LIVE_HUMIDITY_LOWER
        HUMIDITY_UPPER = LIVE_HUMIDITY_UPPER
        show_lcd("Live mode", "T" + str(TEMP_LOWER) + "-" + str(TEMP_UPPER) + "C H" + str(HUMIDITY_LOWER) + "-" + str(HUMIDITY_UPPER) + "%")
        print("Live mode - temperature range " + str(TEMP_LOWER) + "-" + str(TEMP_UPPER) + " deg C, humidity range " + str(HUMIDITY_LOWER) + "-" + str(HUMIDITY_UPPER) + " %RH")
        sleep(2)

    else:
        #shows the current reading before asking for a range, so it can be set around the
        #real room conditions instead of guessing - also sends the same reading to Telegram
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
        print("Pick a low and high limit so this reading sits BETWEEN them to count as good, or put both limits to one side of it (both above, or both below) to make it count as bad.")
        sent = send_telegram_alert("Smart Medical Locker - demo setup. The locker currently reads " + reading_text + ". Choose the temperature and humidity range around these values.")
        if sent:
            print("(the same reading has been sent to Telegram)")
            lcd_line2 = "Sent to Telegram"
        else:
            lcd_line2 = "No network"

        show_lcd("Now T" + str(setup_temp) + " H" + str(setup_humidity), lcd_line2) #held on screen so it can be copied down before typing the range
        sleep(5)

        if is_startup and len(sys.argv) > 5:
            TEMP_LOWER = int(sys.argv[2])
            TEMP_UPPER = int(sys.argv[3])
            HUMIDITY_LOWER = int(sys.argv[4])
            HUMIDITY_UPPER = int(sys.argv[5])
        else:
            TEMP_LOWER = read_number_from_keypad("TempLo? #=ok")
            TEMP_UPPER = read_number_from_keypad("TempHi? #=ok")
            HUMIDITY_LOWER = read_number_from_keypad("HumLo? #=ok")
            HUMIDITY_UPPER = read_number_from_keypad("HumHi? #=ok")

        show_lcd("Demo mode", "T" + str(TEMP_LOWER) + "-" + str(TEMP_UPPER) + "C H" + str(HUMIDITY_LOWER) + "-" + str(HUMIDITY_UPPER) + "%")
        print("Demo mode - temperature range " + str(TEMP_LOWER) + "-" + str(TEMP_UPPER) + " deg C, humidity range " + str(HUMIDITY_LOWER) + "-" + str(HUMIDITY_UPPER) + " %RH")
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
        #while idle, * reopens the menu so the mode or range can be changed without using the terminal
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
            #card and PIN are correct, but a breach is active, so the vault stays locked anyway
            show_lcd("VAULT LOCKED", "Breach detected")
            send_telegram_alert("Access blocked: valid card " + uid + " but a breach is active")
            blink_led(STATUS_LED_PIN, 5)
            sound_buzzer(1)
            sleep(1.5) #let the breach message stay readable before resetting
            show_lcd("Vault Secured", "Tap card...")

        else:
            if warning_active:
                #only 1 or 2 of the 3 are bad, not a full breach - show a warning for 2 seconds, then unlock anyway
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
                    break #manual early close - user pressed * instead of waiting out the 10s window
                sleep(0.1) #10 second open window

            lock_servo()
            vault_is_open = False
            GPIO.output(STATUS_LED_PIN, 0)
            show_lcd("Vault Secured", "Tap card...")

    #waits until the card is lifted before scanning again - otherwise a card left on the
    #reader gets read again straight away and the screen jumps back to "Enter PIN" by
    #itself, which looks like it has frozen. Sensors are still checked while we wait.
    while reader.read_id_no_block() is not None:
        poll_sensors_and_check_breach()
        sleep(0.2)

    sleep(0.1)
