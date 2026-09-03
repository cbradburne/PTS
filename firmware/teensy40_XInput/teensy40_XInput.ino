//https://github.com/dmadison/ArduinoXInput
//https://github.com/dmadison/ArduinoXInput_Teensy
//https://hardwaretester.com/gamepad

// I used the older Arduino IDE v1.58 along with the older Teensyduino

#include <XInput.h>

// Setup
const boolean UseLeftJoystick   = true;  // set to true to enable left joystick
const boolean InvertLeftYAxis   = true;  // set to true to use inverted left joy Y

const boolean UseRightJoystick  = true;  // set to true to enable right joystick
const boolean InvertRightYAxis  = true;  // set to true to use inverted right joy Y

const boolean UseTriggerButtons = true;   // set to false if using analog triggers

const int ADC_Max = 1023;  // 10 bit

// Joystick Pins
const int Pin_LeftJoyX  = 23;
const int Pin_LeftJoyY  = 22;
const int Pin_RightJoyX = 20;
const int Pin_RightJoyY = 21;

// Button Pins
const int Pin_ButtonA = 2;

const int deadRange = 100;

int XdeadRangeLow = (512 - deadRange);
int XdeadRangeHigh = (512 + deadRange);
int YdeadRangeLow = (512 - deadRange);
int YdeadRangeHigh = (512 + deadRange);
int ZdeadRangeLow = (512 - deadRange);
int ZdeadRangeHigh = (512 + deadRange);
int WdeadRangeLow = (512 - deadRange);
int WdeadRangeHigh = (512 + deadRange);

int X_min = 2;
int X_max = 1023;
int Y_min = 2;
int Y_max = 1023;
int Z_min = 2;
int Z_max = 1023;
int W_min = 2;
int W_max = 1023;

short XShort = 0;
short YShort = 0;
short ZShort = 0;
short WShort = 0;

bool debug = false;

void setup() {
  // Set buttons as inputs, using internal pull-up resistors
  pinMode(Pin_ButtonA, INPUT_PULLUP);

  XInput.setJoystickRange(0, ADC_Max);  // Set joystick range to the ADC
  XInput.setAutoSend(false);  // Wait for all controls before sending

  XInput.begin();
}

void loop() {
  // Read pin values and store in variables
  // (Note the "!" to invert the state, because LOW = pressed)
  boolean buttonA = !digitalRead(Pin_ButtonA);

  // Set XInput buttons
  XInput.setButton(BUTTON_A, buttonA);

  // Set left joystick
  if (UseLeftJoystick == true) {
    int leftJoyX = analogRead(Pin_LeftJoyX);
    int leftJoyY = analogRead(Pin_LeftJoyY);
    if (debug) {
      XShort = leftJoyX;
    }
    else {
      if (leftJoyX < XdeadRangeLow) {
        XShort = map(leftJoyX, X_min, XdeadRangeLow, 0, 512);
        //Serial.println(XShort);
        if (XShort < 0) {
          XShort = 0;
        }
      } else if (leftJoyX > XdeadRangeHigh) {
        XShort = map(leftJoyX, XdeadRangeHigh, X_max, 512, 1023);
        //Serial.println(XShort);
        if (XShort > 1023) {
          XShort = 1023;
        }
      } else {
        XShort = 512;
      }
    }

    if (debug) {
      YShort = leftJoyY;
    }
    else {
      if (leftJoyY < YdeadRangeLow) {
        YShort = map(leftJoyY, Y_min, YdeadRangeLow, 0, 512);
        //Serial.println(YShort);
        if (YShort < 0) {
          YShort = 0;
        }
      } else if (leftJoyY > YdeadRangeHigh) {
        YShort = map(leftJoyY, YdeadRangeHigh, Y_max, 512, 1023);
        //Serial.println(YShort);
        if (YShort > 1023) {
          YShort = 1023;
        }
      } else {
        YShort = 512;
      }
    }

    boolean invert = !InvertLeftYAxis;

    XInput.setJoystickX(JOY_LEFT, XShort);
    XInput.setJoystickY(JOY_LEFT, YShort, invert);
  }

  // Set right joystick
  if (UseRightJoystick == true) {
    int rightJoyX = analogRead(Pin_RightJoyX);
    int rightJoyY = analogRead(Pin_RightJoyY);

    if (debug) {
      ZShort = rightJoyX;
    }
    else {
      if (rightJoyX < ZdeadRangeLow) {
        ZShort = map(rightJoyX, Z_min, ZdeadRangeLow, 0, 512);
        //Serial.println(ZShort);
        if (ZShort < 0) {
          ZShort = 0;
        }
      } else if (rightJoyX > ZdeadRangeHigh) {
        ZShort = map(rightJoyX, ZdeadRangeHigh, Z_max, 512, 1023);
        //Serial.println(ZShort);
        if (ZShort > 1023) {
          ZShort = 1023;
        }
      } else {
        ZShort = 512;
      }
    }

    if (debug) {
      WShort = rightJoyY;
    }
    else {
      if (rightJoyY < WdeadRangeLow) {
        WShort = map(rightJoyY, W_min, WdeadRangeLow, 0, 512);
        //Serial.println(WShort);
        if (WShort < 0) {
          WShort = 0;
        }
      } else if (rightJoyY > WdeadRangeHigh) {
        WShort = map(rightJoyY, WdeadRangeHigh, W_max, 512, 1023);
        //Serial.println(WShort);
        if (WShort > 1023) {
          WShort = 1023;
        }
      } else {
        WShort = 512;
      }
    }

    boolean invert = !InvertRightYAxis;

    XInput.setJoystickX(JOY_RIGHT, ZShort);
    XInput.setJoystickY(JOY_RIGHT, WShort, invert);
  }

  // Send control data to the computer
  XInput.send();
}
