import serial
import time

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
req = ["HH:60", "HM+60"]


class Arduino:
    # /dev/ttyUSB0
    def __init__(self, ser):
        self.baud_rate = 115200
        self.ser = serial.Serial(ser, self.baud_rate, bytesize=8, timeout=2)
        self.ser.readlines()

    def send_commands(self, formatted_commands):
        self.ser.write(bytes(formatted_commands, "ASCII")) 
        while True:
            msg = self.ser.readline()
            print(msg)
            if msg == b'':
                break

    def move_head(self, operation, degree):
        # TODO: Create verification
        self.ser.write(bytes(f"HH{operation}{degree}\n", "ASCII"))
        self.ser.readline()


class Request:
    def verify_commands(self, commands):
        try:
            return commands["commands"]

        except Exception as E:
            print(E)
            self.send_error("100")

    def send_error(self, error_code):
        """
        Error code will come from Arduino and map to error message
        from server dictionary
        """
        print(error_code)

    def format_commands(self, processed_commands):
        if not processed_commands:
            self.send_error("100")
        
        if len(processed_commands) > 1:
            return "\n".join(processed_commands)
        
        return processed_commands[0] + "\n"

    def post(self, commands):
        processed_commands = self.format_commands(self.verify_commands(commands))
        print(f"COMMANDS:\n{processed_commands}\n")
        return processed_commands


class GaryAPI:
    def __init__(self):
        self.serial = "/dev/ttyUSB0"

    def send_commands(self, commands):
        self.api = Request()
        comms = self.api.post(commands)
        self.arduino = Arduino(self.serial)
        self.arduino.send_commands(comms)

    def set_serial(self, ser):
        self.serial = ser
        return self
