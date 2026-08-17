import serial #pip3 install pyserial
from serial import tools as serial_tools
import subprocess
import datetime
import json
#import cv2
import asyncio
from pin_and_servos import machinations

import aiy.voice.tts

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
    def __init__(self, ser):
        self.baud_rate = 115200
        self.ser = serial.Serial(ser, self.baud_rate, bytesize=8, timeout=2)
        
        self.ser.readlines()

    def send_commands(self, formatted_commands):
        self.ser.write(formatted_commands.encode("ASCII"))
        res = Request()
        while True:
            msg = self.ser.readline()

            if msg == b"":
                return res.send_success()

            if msg.startswith(b"E100"):
                return res.send_error(100)
            #DEBUG - uncomment this to see all messages from Arduino
            #else:
                #print(msg)
    
    @staticmethod
    def available_machinations():
        return machinations


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
        errors = {
            "100": "Invalid servo",
            "101": "Invalid commands",
            "111": "Camera failed",
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
        aiy.voice.tts.say(text, lang='en-GB', volume=50, pitch=69, speed=125)
        #subprocess.call('espeak -s200 -a70 "' + text + '" 2>/dev/null', shell=True)
        # -s is speed
        # -a is volume (amplitude)
        # see [eSpeak Docs](http://espeak.sourceforge.net/commands.html) for more customization

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