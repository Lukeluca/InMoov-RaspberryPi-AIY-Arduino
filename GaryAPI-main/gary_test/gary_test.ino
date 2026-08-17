#include <Servo.h>

Servo my_servo;
long amount;

void setup() {
  
  Serial.begin(9600);
  my_servo.attach(9);
}

void move_servo(long pos, Servo serv) {
  Serial.println(pos);
  for (int i = 0; i <= pos; i += 1){
      serv.write(i);
    }
}

void loop() {
  
  
  if(Serial.available()> 0){
      String line = Serial.readString(); // read a string from the serial port
 
      if (line.startsWith("SS")) {
          line.remove(0, 3);
          line.trim();
          amount = line.toInt();
          Serial.println(amount);
          move_servo(amount, my_servo);
          line = "";
      }
      Serial.println("");
  }

}
