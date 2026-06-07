use rppal::i2c::I2c;
use ssd1306::{prelude::*, I2CDisplayInterface, Ssd1306};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let i2c = I2c::with_bus(1)?;  // bus 1 is the standard Pi I2C pins
    let interface = I2CDisplayInterface::new(i2c);
    let mut display = Ssd1306::new(interface, DisplaySize128x64, DisplayRotation::Rotate0)
        .into_buffered_graphics_mode();

    display.init()?;

    // ... your drawing code ...

    display.flush()?;
    Ok(())
}