void Serial5Data() {
  if (Serial5.available()) {                                    // If anything comes in Serial5 (pins 0 & 1)
    char g = Serial5.read();
    if (g == '#') {
      while (Serial5.available() < 1) {                         //  Wait for 1 byte to be available.
        delayMicroseconds(200);
      }
      char h = Serial5.read();
      if (h == 'Z') { Serial.println("~511"); }
      else if (h == 'X') { Serial.println("~521"); }
      else if (h == 'C') { Serial.println("~531"); }
      else if (h == 'V') { Serial.println("~541"); }
      else if (h == 'B') { Serial.println("~551"); }
      else if (h == 'N') { Serial.println("~561"); }
      else if (h == 'M') { Serial.println("~571"); }
      else if (h == '<') { Serial.println("~581"); }
      else if (h == '>') { Serial.println("~591"); }
      else if (h == '?') { Serial.println("~501"); }
      else if (h == 'A') { Serial.println("~512"); }
      else if (h == 'S') { Serial.println("~522"); }
      else if (h == 'D') { Serial.println("~532"); }
      else if (h == 'F') { Serial.println("~542"); }
      else if (h == 'G') { Serial.println("~552"); }
      else if (h == 'H') { Serial.println("~562"); }
      else if (h == 'J') { Serial.println("~572"); }
      else if (h == 'K') { Serial.println("~582"); }
      else if (h == 'L') { Serial.println("~592"); }
      else if (h == ':') { Serial.println("~502"); }
      else if (h == 'z') { Serial.println("~513"); }
      else if (h == 'x') { Serial.println("~523"); }
      else if (h == 'c') { Serial.println("~533"); }
      else if (h == 'v') { Serial.println("~543"); }
      else if (h == 'b') { Serial.println("~553"); }
      else if (h == 'n') { Serial.println("~563"); }
      else if (h == 'm') { Serial.println("~573"); }
      else if (h == ',') { Serial.println("~583"); }
      else if (h == '.') { Serial.println("~593"); }
      else if (h == '/') { Serial.println("~503"); }
      else if (h == 'p') { Serial.println("~514"); }
      else if (h == 'P') { Serial.println("~524"); }
      else if (h == 'O') { Serial.println("~515"); }
      else if (h == 'o') { Serial.println("~505"); }
      else if (h == 'a') { Serial.println("~500"); }
      else if (h == 's') { Serial.println("~*"); }
      else if (h == '0') {
        Serial.println("~100");
        Serial.println("~200");
        Serial.println("~300");
        Serial.println("~400");
        Serial.println("~500");
      }
      else if (h == 'd') {
        String stringText = Serial5.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=g1");
        Serial.println(SerialCommandValueInt);
      }
      else if (h == 'f') {
        String stringText = Serial5.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=g2");
        Serial.println(SerialCommandValueInt);
      }
      else if (h == 'g') {
        String stringText = Serial5.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=g3");
        Serial.println(SerialCommandValueInt);
      }
      else if (h == 'h') {
        String stringText = Serial5.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=g4");
        Serial.println(SerialCommandValueInt);
      }
      else if (h == 'j') {
        String stringText = Serial5.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=G1");
        Serial.println(SerialCommandValueInt);
      }
      else if (h == 'k') {
        String stringText = Serial5.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=G2");
        Serial.println(SerialCommandValueInt);
      }
      else if (h == 'l') {
        String stringText = Serial5.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=G3");
        Serial.println(SerialCommandValueInt);
      }
      else if (h == ';') {
        String stringText = Serial5.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=G4");
        Serial.println(SerialCommandValueInt);
      }
      else if (h == 'q') {
        String stringText = Serial5.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=g0");
        Serial.println(SerialCommandValueInt);
      }
      else if (h == 'Q') {
        String stringText = Serial5.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=G0");
        Serial.println(SerialCommandValueInt);
      }
      else if (h == 't') {
        String stringText = Serial5.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=g5");
        Serial.println(SerialCommandValueInt);
      }
      else if (h == 'w') {
        String stringText = Serial5.readStringUntil('\n');
        SerialCommandValueInt = stringText.toInt();
        Serial.print("=G5");
        Serial.println(SerialCommandValueInt);
      }
      else if (h == '+') {
        Serial.println("=-5+");
        cam5Alive = true;
        previousCam5Alive = timeElapsed;
      }
      else if (h == 'i') {
        Serial.println("~505");
      }
      else if (h == 'I') {
        Serial.println("~515");
      }
    }
    else if (g == '^') {
      while (Serial5.available() < 2) {                        //  Wait for 1 byts to be available
        delayMicroseconds(200);
      }
      g = Serial5.read();
      if (g == '=') {
        cam5SlSpeed = Serial5.read();
        cam5SlSpeed -= 48;
        Serial.print("=5");
        Serial.println(cam5SlSpeed);
      }
      else if (g == '@') {
        cam5PTSpeed = Serial5.read();
        cam5PTSpeed -= 48;
        Serial.print("=@5");
        Serial.println(cam5PTSpeed);
      }
    }
    else if (g == 'W') { ; }
    else if (g == '\n') { ; }
    else if (g == '\r') { ; }
    else if (g == '?') { String readSerial5 = Serial5.readStringUntil('\n'); }

    else if (g == 4) {
      delay(1);
      while (Serial5.available() < 9) {                        //  Wait for 6 bytes to be available. Breaks after ~20ms if bytes are not received.
        delayMicroseconds(200);
      }
      char readSerialC5 = Serial5.read();                     // Read Z
      readSerialC5 = Serial5.read();                          // Read Z
      readSerialC5 = Serial5.read();                          // Read X
      readSerialC5 = Serial5.read();                          // Read X
      readSerialC5 = Serial5.read();                          // Read Y
      readSerialC5 = Serial5.read();                          // Read Y
      readSerialC5 = Serial5.read();                          // Read W
      readSerialC5 = Serial5.read();                          // Read W
      readSerialC5 = Serial5.read();                          // Read Camera ID
    }

    else {
      inData5 = "\nCam5:\n";
      inData5 += g;
      delay(1);
      while (Serial5.available() > 0) {
        inData5 += Serial5.readStringUntil('\n');
        if (inData5 == "#$\r") {
          break;
        }
        Serial.println(inData5);
        inData5 = "";
        delay(2);
      }
    }
  }
}
