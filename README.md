# BTLparser part 1

CX Supervisor script for parsing BTL files (CNC machining and CAD file) converted to python. Offloads computing within Supervisor runtime to windows.  

Large files which take several seconds of 100% cpu usage can be parsed within miliseconds using this script.  

Script generates FileARR and ElemFileARR text files corresponding to a 'Set'. All required parameters are extracted and mapped to part series number. These files can then be easily read in supervisor.  

# BTLparser part 2

Second part of BTL file parse. Extracts all process lines for each part in BTL file.  

Requires mapping.txt file for mapping each Element to a ProjectID and BuildingID. In manual mode these fields are empty and BTL file path is passed as argument from Supervisor. This mapping file is updated at the end to map the generated process files to each Element.  
Multiple Elements can share a single BTL file (generated process file for each BTL file) and there can be Elements from a different BTL file in the same series. Mapping is required for Supervisor to read the correct file.  

On Supervisor, processes for each required part can be extracted and used to generate PLC work file. This program offloads reading and looping through multiple large files from Supervisor to Windows, making the process noticeably faster.
