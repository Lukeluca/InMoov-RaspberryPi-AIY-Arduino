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
            else:
                print(msg)