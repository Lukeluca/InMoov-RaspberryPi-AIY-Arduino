void setup() {
  Serial.begin(9600);
}

void loop() {
  if(Serial.available()){
      String line = Serial.readString(); // read a string from the serial port
      Serial.println(line);
  }

}
