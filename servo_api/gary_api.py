import serial #pip3 install pyserial
from serial import tools as serial_tools
import subprocess
import datetime
import json
#import cv2
import asyncio
import threading

import aiy.voice.tts

import tts

# TODO: Send Gary data
# -- Sending him movement information
# -- define the request paramters
# -- method of request verification

# TODO: Get data from Gary
# -- Websocket use
# -- Connecting to Arduino
# Requesting from Arduino

"""
int getControlPin(char servo[]) {
  if (strcmp(servo,"HH") == 0) return PIN_HEAD_TWIST;
  if (strcmp(servo,"HV") == 0) return PIN_HEAD_VERT;
  if (strcmp(servo,"HM") == 0) return PIN_HEAD_MOUTH;

  if (strcmp(servo,"RB") == 0) return PIN_RIGHT_BICEP_TWIST;
  if (strcmp(servo,"RT") == 0) return PIN_RIGHT_THUMB;
  
  if (strcmp(servo,"RI") == 0) return PIN_RIGHT_INDEX;
  if (strcmp(servo,"RM") == 0) return PIN_RIGHT_MIDDLE;
  if (strcmp(servo,"RR") == 0) return PIN_RIGHT_RING;
  if (strcmp(servo,"RP") == 0) return PIN_RIGHT_PINKY;

  if (strcmp(servo,"RW") == 0) return PIN_RIGHT_WRIST;

  // error, none found
  return -1;
}
"""

## HH:60\n
## HH+60
# req = ["HH:60", "HM+60"]


class Arduino:
    # /dev/ttyUSB0

    # The serial port is a single shared resource, but Flask serves requests on
    # multiple threads and the brain fires mouth commands as fire-and-forget
    # threads, so two writes can overlap. When that happens one thread's
    # readline() swallows the bytes the other is waiting for, and pyserial
    # raises "device reports readiness to read but returned no data". The lock
    # is on the class rather than the instance so it still holds if a second
    # Arduino is constructed, as the Sight class does.
    _serial_lock = threading.Lock()

    # The firmware prints exactly one "C:" echo per newline it receives. That
    # echo is the terminator this read loop used to lack: counting echoes says
    # the batch has been parsed, so we no longer learn it by waiting for the
    # port to fall silent.
    #
    # Why that mattered. The old loop read until readline() came back empty -
    # until the read timed out - so every command cost the full timeout of
    # 0.25s, while the Arduino had in fact answered in about 14ms. Four
    # commands a second was the whole robot's ceiling; smooth motion needs
    # twenty-five.
    #
    # This is now only a liveness guard - how long to wait for a firmware that
    # is not answering at all - and costs nothing when it is answering, because
    # readline() returns as soon as the newline arrives.
    ECHO_TIMEOUT = 0.25

    # The per-command "M:" and "E1xx:" lines follow the echo rather than
    # preceding it, so returning the moment the last echo lands would leave
    # them in the buffer for the next command's read to mistake for its own -
    # the same way the boot banner would if it were not drained. They are
    # already on the wire by then, about 3ms each at 115200, so this bounds the
    # gap between two lines rather than the whole drain.
    DRAIN_TIMEOUT = 0.02

    # Opening the port resets the Arduino, which then takes a second or two to
    # boot and print its banner. Draining that needs much longer than a command
    # reply, or the banner leaks into the first real command's response.
    BOOT_TIMEOUT = 2.0

    def __init__(self, ser):
        self.baud_rate = 115200
        self.ser = serial.Serial(ser, self.baud_rate, bytesize=8,
                                 timeout=self.BOOT_TIMEOUT)

        self.ser.readlines()
        # Banner drained; from here on a command should not cost two seconds.
        self.ser.timeout = self.ECHO_TIMEOUT

    def send_commands(self, formatted_commands):
        with Arduino._serial_lock:
            return self._send_commands_locked(formatted_commands)

    def _send_commands_locked(self, formatted_commands):
        # One echo per newline, blank lines included: the firmware treats any
        # newline as a complete line and answers with "C:" plus whatever it
        # held, even nothing. A payload not ending in a newline has no echo
        # coming for its last fragment, which is a caller's bug rather than
        # something to sit and wait out.
        expected_echoes = formatted_commands.count("\n")

        # Anything still unread belongs to a batch that has already returned -
        # a previous call that gave up on a wedged firmware, say - and would
        # otherwise be counted as this batch's replies.
        self.ser.reset_input_buffer()
        self.ser.write(formatted_commands.encode("ASCII"))

        res = Request()
        error_code = None
        echoes = 0

        while echoes < expected_echoes:
            msg = self.ser.readline()
            #DEBUG - uncomment this to see all messages from Arduino
            #print(msg)

            if msg == b"":
                # Timed out with echoes outstanding: the firmware is wedged, or
                # the board is resetting. Say so rather than reporting a move
                # that may not have happened.
                return res.send_error(112)

            if msg.startswith(b"C:"):
                echoes += 1
            else:
                error_code = self._error_code(msg) or error_code

        # The trailing per-command lines for the final echo.
        self.ser.timeout = self.DRAIN_TIMEOUT
        try:
            while True:
                msg = self.ser.readline()
                if msg == b"":
                    break
                error_code = self._error_code(msg) or error_code
        finally:
            self.ser.timeout = self.ECHO_TIMEOUT

        if error_code is not None:
            return res.send_error(error_code)
        return res.send_success()

    @staticmethod
    def _error_code(line):
        """The number in an "E1xx:" line, or None if it is not an error.

        The firmware reports four: an unknown servo, a missing value, a servo
        in its table but not enabled, and a line too short to hold a code. The
        old loop matched E100 alone, so a command rejected for any of the other
        three came back as a success.
        """
        if not line.startswith(b"E1"):
            return None
        try:
            return int(line[1:4])
        except ValueError:
            return None


class Request:
    def __init__(self):
        self.code = 200
        self.response = "SUCCESS"
        self.has_error = False

    def verify_commands(self, commands):
        try:
            return commands["commands"]

        except Exception as E:
            print(E)
            self.has_error = True
            return self.send_error(101)

    def send_error(self, error_code):
        """
        Error code will come from Arduino and map to error message
        from server dictionary
        """
        # 100 to 103 come from the firmware. 111 and 112 are ours: nothing on
        # the Arduino knows about the camera, and a firmware that does not
        # answer cannot report that it did not answer.
        errors = {
            "100": "Invalid servo",
            "101": "Missing value, or invalid commands",
            "102": "Servo not enabled",
            "103": "Malformed command",
            "111": "Camera failed",
            "112": "Arduino did not reply",
            "unknown": "Unknown error",
        }

        return {
            "error_message": error_code,
            "response": "ERROR: " + errors.get(str(error_code), errors["unknown"]),
        }

    def format_commands(self, processed_commands):
        return "\n".join(processed_commands) + "\n"

    def post(self, commands):
        verify_commands = self.verify_commands(commands)
        processed_commands = self.format_commands(verify_commands)
        #uncomment to see commands sent to Arduino
        #print(f"COMMANDS:\n{processed_commands}\n")
        return processed_commands

    def send_success(self):
        return {"error_message": self.code, "response": self.response}


class GaryAPI:
    def __init__(self):
        self.serial = "/dev/ttyUSB0"
        self.arduino = Arduino(self.serial)

    def send_commands(self, commands):
        self.api = Request()
        comms = self.api.post(commands)
        #self.arduino = Arduino(self.serial)
        sent = self.arduino.send_commands(comms)

        if self.api.has_error:
            return comms

        if sent["response"].startswith("ERROR"):
            return sent

        return self.api.send_success()

    def set_serial(self, ser):
        self.serial = ser
        return self
    
    @property
    def sight(self):
        return Sight()
    
    @property
    def speech(self):
        return Speech()

            
class Speech:

    def __init__(self):
        pass
        
    def text2speech(self, text):
        # Which engine, and why file rather than streaming, is all in tts.py.
        tts.speak(text)

'''
class Sight:

    def __init__(self):
        self.vc = cv2.VideoCapture(-1)
        self.frame_path = './static/images/frame.jpg'
        self.font = cv2.FONT_HERSHEY_SIMPLEX
        self.faceCascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        self.ard = Arduino("/dev/ttyUSB0")
    
    def read_cam(self):
        if self.vc.isOpened():
            ret, frame = self.vc.read()
            return ret, frame
    
    def write_frame(self, frame):
        cv2.imwrite(self.frame_path, frame)
    
    
    def release(self):
        self.vc.release()
        #self.vc.destroyAllWindows()

    def capture(self):
        ret, frame = self.read_cam()
        if ret == False:
            return json.dumps(Request().send_error(111))
        
        self.write_frame(frame)
        self.release()
        
        return self.frame_path
        
    def streamVideo(self, params = {}):
        while True:
            try:
                #print("getting frame")
                ret, frame = self.read_cam()
            except Exception as e:
                self.release()
                return json.dumps(Request().send_error(111))

            if ret == False:
                return json.dumps(Request().send_error(111))
            
            cv2.putText(frame, 'SCANNING... - ' +  str(datetime.datetime.now()) , (0,30), self.font, 1, (200, 255, 155))
            self.write_frame(frame)

            if params.get('detect_faces', False):
                self.detect_faces(frame, move_to_face=params.get('move_to_face', False))
           
            yield (b'--frame\r\n' 
              b'Content-Type: image/jpeg\r\n\r\n' + open(self.frame_path, 'rb').read() + b'\r\n')

    
    def detect_faces(self, frame, move_to_face = True):
            # Getting corners around the face
            # 1.3 = scale factor, 5 = minimum neighbor can be detected
            faces = self.faceCascade.detectMultiScale(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), 1.3, 5)
            moved = False
            for (x, y, w, h) in faces:

                img = cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 0), 3)
                cv2.putText(img, 'HUMAN DETECTED' , (x,y), self.font, 0.6, (200, 255, 155))
                self.write_frame(frame)
                    
                if w > 0 and moved == False:
                    if move_to_face == True:
                        self.move_head_to_position(x, y)
                        moved = True
    
    def move_head_to_position(self, x, y):
        HEAD_TWIST = "HH"
        HEAD_VERTICAL = "HV"
        
        #Dead Zone (where Gary won't reposition)
        x_min = 270
        x_max = 320
        y_min = 150
        y_max = 225
        
        if (x > x_min and x < x_max and y > y_min and y < y_max): return
        #don't reposition between 200<x<400 and 125<y<250
        
        #print("found face at x:" + str(x) + " y:" + str(y))
        
        x_servo_pos = None
        y_servo_pos = None
        if (x > x_max):
            x_servo_pos = -8
        if (x < x_min):
            x_servo_pos = 8
            HEAD_TWIST = "HH+"
        if (y > y_max):
            y_servo_pos = -8 #y starts at top, tilt head down
        if (y < y_min):
            y_servo_pos = +8
            HEAD_VERTICAL = "HV+"

        
        if (x_servo_pos is not None and y_servo_pos is not None):
            #print("adjust both")
            format_r = Request().format_commands([f"{HEAD_TWIST}{x_servo_pos}", f"{HEAD_VERTICAL}{y_servo_pos}"])
        else:
            if (x_servo_pos is not None):
                #print("adjust horizontal")
                format_r = Request().format_commands([f"{HEAD_TWIST}{x_servo_pos}"])
            else:
                #print ("adjust vertical")
                format_r = Request().format_commands([f"{HEAD_VERTICAL}{y_servo_pos}"])
 
        #print("starting command")
        #self.release()
        self.ard.send_commands(format_r)
        #self.vc = cv2.VideoCapture(-1)
        #print("command complete")
    '''