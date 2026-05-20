# BTLparser

CX Supervisor script for parsing BTL files (CNC machining and CAD file) converted to python. Offloads computing within Supervisor runtime to windows.  

Large files which take several seconds of 100% cpu usage can be parsed within miliseconds using this script.  

Script generates FileARR and ElemFileARR text files corresponding to a 'Set'. All required parameters are extracted and mapped to part series number. These files can then be easily read in supervisor.  

Next iteration will have extraction of all required processes from an optimized part cut order.
