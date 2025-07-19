
# TLG Blender Importer

The tool is designed to handle the game's custom .BOD (model/skeleton) and .DATA(animation) file formats


## Requirements

Blender: Version 4.5 or newer (might work on older versions too).

GNF to DDS Converter: You must have a copy of [\_\_From_GNF_To_DDS_DXT5__GFDLibrary_.exe](https://github.com/JADERLINK/ImageConvert/tree/main) or a similar GNF-to-DDS conversion tool. The add-on calls this executable to convert textures on the fly.

Paths: The importer attempts to automatically locate the TEXTURES directory based on the path of the imported .BOD file. For this to work, you should maintain the game's original `GAME/ASSETS/` and `GAME/TEXTURES/` directory structure.

## Installation

1. Download the latest `tlg_importer.zip` file from the Releases page.
2. Open Blender and go to `Edit > Preferences > Add-ons`.
3. Click the Install... button and select the .zip file you downloaded.
4. Enable the add-on by checking the box next to "Import-Export: The Last Guardian Importer".

## Configuration
Before you can import models with textures, you must tell the add-on where to find the GNF converter.

1. In Blender, go to `Edit > Preferences > Add-ons`.

2. Find "The Last Guardian Importer" in the list and expand it.

3. In the Preferences section, click the folder icon next to GNF to DDS Converter .exe and navigate to where you have saved the `__From_GNF_To_DDS_DXT5__GFDLibrary_.exe` file.

4. Save your preferences. The add-on is now ready to use.

## How To Use

### Importing a Skeleton (.BOD)
1. Go to `File > Import > The Last Guardian (.bod)`.
2. Navigate to the directory containing the Skeleton `.BOD`file.
3. Select the Skeleton `.BOD` file you wish to import.
4. Click Import TLG Model.

### Importing a Model (.BOD)
1. Select the previously imported skeleton in Object Mode.
2. Go to `File > Import > The Last Guardian (.bod)`.
3. Navigate to the directory containing the Model `.BOD` file.
4. Select the model `.BOD` file you wish to import.
5. Click Import TLG Model. The importer will automatically load the model, materials, and skinning data.

### Importing an Animation (.DATA)
Important: You must import and select the model's skeleton before importing an animation.

1. Import a model (.bod file) as described above.
2. In the 3D Viewport, select the armature object you want to apply the animation to.
3. Go to `File > Import > The Last Guardian Animation (.data)`.
4. Navigate to and select the .DATA animation file.
5. The animation will be loaded as a new Action and applied to the selected armature.
(You maybe have to reselect the created action in the dope sheet to see the animation)

## Notes
-Materials are not 100% accurate, normal maps dont work correctly and strength has been set to 0.0. 
-Some textures might not import correctly and need manual fixing. 
