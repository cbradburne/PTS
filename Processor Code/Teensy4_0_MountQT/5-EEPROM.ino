//  EEPROM

void saveEEPROM(void) {
  EEPROM.put(EEPROM_ADDRESS_PANTILT_SET_SPEED, pantilt_set_speed);
  EEPROM.put(EEPROM_ADDRESS_SLIDER_SET_SPEED, slider_set_speed);
  EEPROM.put(EEPROM_ADDRESS_SLIDE_LIMIT, slideLimit);
  EEPROM.put(EEPROM_ADDRESS_ZOOM_LIMIT, zoomLimit);
  EEPROM.put(EEPROM_ADDRESS_PANTILT_ACCEL, pantilt_accel);
  EEPROM.put(EEPROM_ADDRESS_SLIDER_ACCEL, slider_accel);
  EEPROM.put(EEPROM_ADDRESS_PANTILT_SPEED1, pantilt_speed1);
  EEPROM.put(EEPROM_ADDRESS_PANTILT_SPEED2, pantilt_speed2);
  EEPROM.put(EEPROM_ADDRESS_PANTILT_SPEED3, pantilt_speed3);
  EEPROM.put(EEPROM_ADDRESS_PANTILT_SPEED4, pantilt_speed4);
  EEPROM.put(EEPROM_ADDRESS_SLIDER_SPEED1, slider_speed1);
  EEPROM.put(EEPROM_ADDRESS_SLIDER_SPEED2, slider_speed2);
  EEPROM.put(EEPROM_ADDRESS_SLIDER_SPEED3, slider_speed3);
  EEPROM.put(EEPROM_ADDRESS_SLIDER_SPEED4, slider_speed4);

  Serial1.println("Saved to EEPROM.\n");
  Serial1.println("#$");
}


/*--------------------------------------------------------------------------------------------------------------------------------------------------------*/


void getEEPROMVariables(void) {
  EEPROM.get(EEPROM_ADDRESS_PANTILT_SET_SPEED, pantilt_set_speed);
  EEPROM.get(EEPROM_ADDRESS_SLIDER_SET_SPEED, slider_set_speed);
  EEPROM.get(EEPROM_ADDRESS_SLIDE_LIMIT, slideLimit);
  EEPROM.get(EEPROM_ADDRESS_ZOOM_LIMIT, zoomLimit);
  EEPROM.get(EEPROM_ADDRESS_PANTILT_ACCEL, pantilt_accel);
  EEPROM.get(EEPROM_ADDRESS_SLIDER_ACCEL, slider_accel);
  EEPROM.get(EEPROM_ADDRESS_PANTILT_SPEED1, pantilt_speed1);
  EEPROM.get(EEPROM_ADDRESS_PANTILT_SPEED2, pantilt_speed2);
  EEPROM.get(EEPROM_ADDRESS_PANTILT_SPEED3, pantilt_speed3);
  EEPROM.get(EEPROM_ADDRESS_PANTILT_SPEED4, pantilt_speed4);
  EEPROM.get(EEPROM_ADDRESS_SLIDER_SPEED1, slider_speed1);
  EEPROM.get(EEPROM_ADDRESS_SLIDER_SPEED2, slider_speed2);
  EEPROM.get(EEPROM_ADDRESS_SLIDER_SPEED3, slider_speed3);
  EEPROM.get(EEPROM_ADDRESS_SLIDER_SPEED4, slider_speed4);

  stepper_pan.setAcceleration(pantilt_accel * (pantilt_set_speed / 10));
  stepper_tilt.setAcceleration(pantilt_accel * (pantilt_set_speed / 10));
  stepper_slider.setAcceleration(slider_accel * (slider_set_speed / 10));
}


/*--------------------------------------------------------------------------------------------------------------------------------------------------------*/


void printEEPROM(void) {
  float ftemp;
  Serial1.println("\nEEPROM:");
  EEPROM.get(EEPROM_ADDRESS_PANTILT_SET_SPEED, ftemp);
  Serial1.println(String("Pan/Tilt Speed      : ") + ftemp + String(" deg/s"));
  EEPROM.get(EEPROM_ADDRESS_SLIDER_SET_SPEED, ftemp);
  Serial1.println(String("Slider Speed        : ") + ftemp + String(" mm/s\n"));
  EEPROM.get(EEPROM_ADDRESS_SLIDE_LIMIT, ftemp);
  Serial1.println(String("Slide Limit         : ") + sliderStepsToMillimetres(ftemp) + String(" mm"));
  EEPROM.get(EEPROM_ADDRESS_ZOOM_LIMIT, ftemp);
  Serial1.println(String("Zoom Limit          : ") + ftemp + String("\n"));
  EEPROM.get(EEPROM_ADDRESS_PANTILT_ACCEL, ftemp);
  Serial1.println(String("Pan/Tilt accel      : ") + ftemp + String(" steps/sSq"));
  EEPROM.get(EEPROM_ADDRESS_SLIDER_ACCEL, ftemp);
  Serial1.println(String("Slider accel        : ") + ftemp + String(" steps/sSq\n"));
  EEPROM.get(EEPROM_ADDRESS_PANTILT_SPEED1, ftemp);
  Serial1.println(String("Pan/Tilt Speed 1    : ") + ftemp + String(" deg/s"));
  EEPROM.get(EEPROM_ADDRESS_PANTILT_SPEED2, ftemp);
  Serial1.println(String("Pan/Tilt Speed 2    : ") + ftemp + String(" deg/s"));
  EEPROM.get(EEPROM_ADDRESS_PANTILT_SPEED3, ftemp);
  Serial1.println(String("Pan/Tilt Speed 3    : ") + ftemp + String(" deg/s"));
  EEPROM.get(EEPROM_ADDRESS_PANTILT_SPEED4, ftemp);
  Serial1.println(String("Pan/Tilt Speed 4    : ") + ftemp + String(" deg/s\n"));
  EEPROM.get(EEPROM_ADDRESS_SLIDER_SPEED1, ftemp);
  Serial1.println(String("Slider Speed 1      : ") + ftemp + String(" mm/s"));
  EEPROM.get(EEPROM_ADDRESS_SLIDER_SPEED2, ftemp);
  Serial1.println(String("Slider Speed 2      : ") + ftemp + String(" mm/s"));
  EEPROM.get(EEPROM_ADDRESS_SLIDER_SPEED3, ftemp);
  Serial1.println(String("Slider Speed 3      : ") + ftemp + String(" mm/s"));
  EEPROM.get(EEPROM_ADDRESS_SLIDER_SPEED4, ftemp);
  Serial1.println(String("Slider Speed 4      : ") + ftemp + String(" mm/s\n"));
  
  Serial1.print("\n");
  Serial1.println("#$");
}