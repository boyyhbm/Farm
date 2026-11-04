'use strict';
const path=require('node:path');
const {spawn}=require('node:child_process');
const {getMakeNsisPath}=require('../../apps/desktop/node_modules/app-builder-lib/out/toolsets/windows.js');
(async()=>{
 const product=process.argv[2];if(!['manager','agent'].includes(product))throw new Error('Unknown product');
 const root=path.resolve(__dirname,'../..');const agent=product==='agent';
 const compiler=await getMakeNsisPath('1.2.1');
 const name=agent?'CS2 Farm Agent':'CS2 Farm Manager';const executable=agent?'CS2-Farm-Agent.exe':'CS2-Farm-Manager.exe';
 const output=path.join(root,'artifacts/windows',product,agent?'CS2-Farm-Agent-Setup.exe':'CS2-Farm-Manager-Setup.exe');
 const prefix=process.platform==='win32'?'/D':'-D';
 const args=[`${prefix}APP_FILES=${path.join(root,'artifacts/windows',product,'win-unpacked','*')}`,`${prefix}APP_DIR=${path.join(root,'artifacts/windows',product,'win-unpacked')}`,`${prefix}SETUP_OUT=${output}`,`${prefix}PRODUCT_NAME=${name}`,`${prefix}APP_ID=${agent?'com.cs2farm.agent':'com.cs2farm.manager'}`,`${prefix}APP_EXE=${executable}`,path.join(root,'build/windows/portable-installer.nsi')];
 const nsisDir=path.join(path.dirname(compiler.path),'windows');
 const nativeWindows=process.platform==='win32'&&compiler.path.endsWith('.cmd');
 const child=spawn(nativeWindows?path.join(nsisDir,'makensis.exe'):compiler.path,args,{stdio:'inherit',env:{...process.env,...compiler.env,...(nativeWindows?{NSISDIR:nsisDir}:{})},shell:false});
 child.on('error',e=>{console.error(e.message);process.exitCode=1;});child.on('exit',code=>process.exitCode=code??1);
})().catch(e=>{console.error(e.message);process.exitCode=1;});
