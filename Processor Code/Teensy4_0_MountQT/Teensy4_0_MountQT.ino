/*
 * Motorised Pan, Tilt, Slide mount.
 * 
 * Using:
 * Teensy 4.0   Development board
 * TMC2209      Stepper motor drivers
 * 
 * Original concept by isaac879 
 * Original project video: https://youtu.be/1FfB7cLkUyQ
 * 
 * Modified by Colin Bradburne
 * 
 * Testing and PCB design Tony McGuire
 * 
 * https://www.pjrc.com/teensy/package_teensy_index.json
 * 
 */


void setup(){
    initPanTilt();
}

void loop(){
    mainLoop();
}