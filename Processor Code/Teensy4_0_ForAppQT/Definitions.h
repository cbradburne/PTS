short shortVals[4] = {0, 0, 0, 0};
short XShort = 0;
short YShort = 0;
short ZShort = 0;
short WShort = 0;
short oldShortVal0 = 0;
short oldShortVal1 = 0;
short oldShortVal2 = 0;
short oldShortVal3 = 0;

String inData1;
String inData2;
String inData3;
String inData4;
String inData5;
String inData6;
String inData7;
String inData8;

String inData1Temp;
String inData2Temp;
String inData3Temp;
String inData4Temp;
String inData5Temp;

int whichSerialCam = 1;

int SerialCommandValueInt;

bool resetLEDs = false;
bool startLEDs = true;
bool doLEDrefresh = false;
bool doLEDrefresh2 = false;
bool doLEDrefresh3 = false;
bool doLEDrefresh4 = false;
bool doLEDrefresh5 = false;
bool doLEDrefresh6 = false;

unsigned long previousMillisLED = 0;
unsigned long currentMillisLED = 0;
unsigned long LEDInterval = 500;

bool LEDstate = LOW;

int cam1SlSpeed = 0;
int cam2SlSpeed = 0;
int cam3SlSpeed = 0;
int cam4SlSpeed = 0;
int cam5SlSpeed = 0;

int cam1PTSpeed = 0;
int cam2PTSpeed = 0;
int cam3PTSpeed = 0;
int cam4PTSpeed = 0;
int cam5PTSpeed = 0;

bool cam1Alive = false;
bool cam2Alive = false;
bool cam3Alive = false;
bool cam4Alive = false;
bool cam5Alive = false;

unsigned long previousCam1Alive = 0;
unsigned long previousCam2Alive = 0;
unsigned long previousCam3Alive = 0;
unsigned long previousCam4Alive = 0;
unsigned long previousCam5Alive = 0;
