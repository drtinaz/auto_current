This service is intended to be used in parallel with a transfer switch service. Two options are available, and they are:
1. Kevin Windrem's Guimods, or
2. My simple transfer switch service (found here https://github.com/drtinaz/transfer_switch) which is not part of guimods. (The transfer switch service has been stripped from guimods to operate on it's own.)
   
   
the purpose of this service is to monitor the outdoor temperature, generator temperature, and altitude. The service then calculates a derated output for the generator based on these inputs.

INSTALL

Before installing, one of the digital inputs should be setup in order to enable/disable the automatic derate function.
In the settings menu of the venus device, set one of the DI to 'Bilge Pump' and change the name to 'Gen Auto Current'.
You can then use the 'invert' option in the device menu to turn the function on or off.

Once you have assigned a digital input for this purpose, install the auto gen current service in ssh by entering the following:
```
wget -O /tmp/download.sh https://raw.githubusercontent.com/drtinaz/auto_current/master/download.sh
bash /tmp/download.sh
```
For a first time installation you will need to edit the config file for your generator specific settings.
