import ctypes
import json
import shutil
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, ttk

import frida

GAME_NAME = "KairoGames.exe"
GAME_SAVES = Path(r"E:\SteamLibrary\steamapps\common\High Sea Saga DX\saves")
BACKUP_ROOT = Path(__file__).resolve().parent / "backups"
SETTINGS_FILE = Path(__file__).resolve().parent / "trainer_settings.json"
PARAM_NAMES = ["HP", "SP", "攻击", "防御", "速度", "智慧"]
PARAM_SUM_LIMIT = 2_000_000_000
# 探索/战斗中攻/防/速会再乘"属性相性"(100±affinity)/100，HP/SP/速度会被技能按百分比放大，
# 两处都是 32 位乘法；合计超过约 1000 万后乘法溢出，会出现打不动怪、防御变负被秒杀、恢复异常等，
# 直接导致探索失败。智慧只有固定加法，不受此限。
PARAM_SAFE_TOTAL = 9_990_000
PARAM_SAFE_LIMITED = ("HP", "SP", "攻击", "防御", "速度")
PARAM_ONECLICK_BASE = 10_000
PARAM_ONECLICK_GROWTH = PARAM_SAFE_TOTAL - PARAM_ONECLICK_BASE
RESOURCE_FIELDS = (("money", "金钱"), ("food", "食物/资源"), ("medals", "奖牌"), ("stamina", "当前体力"))

IL2CPP_SCRIPT = r"""
const game = Process.getModuleByName('GameAssembly.dll');
const POINTER = Process.pointerSize;
function fn(name, ret, args) {
  return new NativeFunction(game.getExportByName(name), ret, args, 'mscdecl');
}
const api = {
  domainGet: fn('il2cpp_domain_get', 'pointer', []),
  domainAssemblies: fn('il2cpp_domain_get_assemblies', 'pointer', ['pointer', 'pointer']),
  assemblyImage: fn('il2cpp_assembly_get_image', 'pointer', ['pointer']),
  classFromName: fn('il2cpp_class_from_name', 'pointer', ['pointer', 'pointer', 'pointer']),
  classFields: fn('il2cpp_class_get_fields', 'pointer', ['pointer', 'pointer']),
  classParent: fn('il2cpp_class_get_parent', 'pointer', ['pointer']),
  fieldName: fn('il2cpp_field_get_name', 'pointer', ['pointer']),
  fieldOffset: fn('il2cpp_field_get_offset', 'uint', ['pointer']),
  fieldStaticGet: fn('il2cpp_field_static_get_value', 'void', ['pointer', 'pointer']),
  objectClass: fn('il2cpp_object_get_class', 'pointer', ['pointer']),
  objectNew: fn('il2cpp_object_new', 'pointer', ['pointer']),
  arrayLength: fn('il2cpp_array_length', 'uint', ['pointer']),
  threadAttach: fn('il2cpp_thread_attach', 'pointer', ['pointer']),
  classMethod: fn('il2cpp_class_get_method_from_name', 'pointer', ['pointer', 'pointer', 'int']),
  runtimeInvoke: fn('il2cpp_runtime_invoke', 'pointer', ['pointer', 'pointer', 'pointer', 'pointer'])
};
const domain = api.domainGet();
api.threadAttach(domain);
function text(pointer) {
  return pointer.isNull() ? null : pointer.readUtf8String();
}
function findClass(namespace, name) {
  const size = Memory.alloc(POINTER);
  const assemblies = api.domainAssemblies(domain, size);
  const namespacePointer = Memory.allocUtf8String(namespace);
  const namePointer = Memory.allocUtf8String(name);
  for (let i = 0; i < size.readU32(); i++) {
    const image = api.assemblyImage(assemblies.add(i * POINTER).readPointer());
    const klass = api.classFromName(image, namespacePointer, namePointer);
    if (!klass.isNull()) return klass;
  }
  throw new Error('找不到类：' + namespace + '.' + name);
}
function getField(klass, name) {
  while (!klass.isNull()) {
    const iterator = Memory.alloc(POINTER);
    iterator.writePointer(NULL);
    for (;;) {
      const field = api.classFields(klass, iterator);
      if (field.isNull()) break;
      if (text(api.fieldName(field)) === name) return field;
    }
    klass = api.classParent(klass);
  }
  throw new Error('找不到字段：' + name);
}
function offset(klass, name) {
  return api.fieldOffset(getField(klass, name));
}
const appClass = findClass('main', 'AppData');
const playerClass = findClass('game', 'Player');
const crewClass = findClass('game', 'Crew');
const characterClass = findClass('game', 'Character');
const humanClass = findClass('game', 'Human');
const itemDataClass = findClass('data', 'ItemData');
const equipDataClass = findClass('data', 'EquipData');
const baseDataClass = findClass('data', 'BaseDataExt');
const sugorokuItemClass = findClass('data', 'SugorokuItem');
const sugorokuDataClass = findClass('data', 'SugorokuData');
const diceClass = findClass('game', 'Dice');
const subFormClass = findClass('form', 'SubForm');
const exIntClass = findClass('ext.util', 'ExInt');
const coinManagerClass = findClass('system.coin.ui', 'CoinManager');
const coinSetClass = findClass('system.coin.ui', 'CoinSet');
const secureLongClass = findClass('kairo.unity.util', 'SecureLong');
const appSingletonField = getField(appClass, 'apdat_');
const playerFieldOffset = offset(appClass, 'player_');
const itemListFieldOffset = offset(appClass, 'itemList_');
const equipListFieldOffset = offset(appClass, 'equipList_');
const sugorokuItemFieldOffset = offset(appClass, 'sugorokuItem_');
const moneyFieldOffset = offset(playerClass, 'money_');
const researchFieldOffset = offset(playerClass, 'researchPoint_');
const staminaFieldOffset = offset(playerClass, 'stamina_');
const humansFieldOffset = offset(appClass, 'humans_');
const diceFieldOffset = offset(playerClass, 'dice_');
const sugorokuPlayNumFieldOffset = offset(playerClass, 'sugorokuPlayNum_');
const dicePowFieldOffset = offset(diceClass, 'dicePow_');
// 双六棋盘实时状态：game.Dice 挂在 Player 上，level_/pos_ 为当前关卡与位置。
// 实测 level_ 是 0 基索引（内部 level 8 -> sugorokuList_[8]，即玩家所见的第 9 关）。
const diceLevelFieldOffset = offset(diceClass, 'level_');
const dicePosFieldOffset = offset(diceClass, 'pos_');
const sugorokuListFieldOffset = offset(appClass, 'sugorokuList_');
const sugorokuGuridMaxOffset = offset(sugorokuDataClass, 'guridMax_');
const sugorokuItemNameOffset = offset(sugorokuItemClass, 'name_');
const nameOffset = offset(crewClass, 'name_');
const levelOffset = offset(crewClass, 'lv_');
const expOffset = offset(crewClass, 'exp_');
const battleHPOffset = offset(crewClass, 'battleHP_');
const battleSPOffset = offset(crewClass, 'battleSP_');
const addParamsOffset = offset(crewClass, 'addParam_');
const paramsOffset = offset(characterClass, 'param_');
const itemNameOffset = offset(itemDataClass, 'name_');
const equipNameOffset = offset(equipDataClass, 'name_');
const itemIdOffset = offset(baseDataClass, 'id_');
const kairoPointFieldOffset = offset(playerClass, 'kairoPoint_');
let stepLockTotal = 0;
let stepLockDiceCount = 1;
let stepLockReady = false;
let stepOverride = null;
let stepOverrideApplied = false;
let stepOverridePos = -1;
let stepOverrideLevel = -1;
function stepPlan(total, count) {
  const base = Math.floor(total / count);
  const extra = total % count;
  const plan = [];
  for (let i = 0; i < count; i++) plan.push(base + (i < extra ? 1 : 0));
  return plan;
}
function applyStepValues(array, total, count) {
  if (array.isNull()) return;
  const plan = stepPlan(total, count);
  const length = api.arrayLength(array);
  for (let i = 0; i < Math.min(length, plan.length); i++) {
    array.add(16 + i * 4).writeS32(plan[i]);
  }
  try {
    const dice = player().add(diceFieldOffset).readPointer();
    const powers = dice.isNull() ? NULL : dice.add(dicePowFieldOffset).readPointer();
    if (!powers.isNull()) {
      for (let i = 0; i < Math.min(api.arrayLength(powers), plan.length); i++) {
        powers.add(16 + i * 4).writeS32(plan[i]);
      }
    }
  } catch (e) {}
}
function applyStepLock(array) {
  if (stepLockTotal <= 0 || array.isNull()) return;
  applyStepValues(array, stepLockTotal, stepLockDiceCount);
}
// 目标格模式：设置后每次投掷前读取实时位置，按“目标格 - 当前格”计算步数写入骰子。
// 超过单掷上限 99 步时保留设置、分多次投掷走过去；到达、越过目标格或切换关卡后自动失效。
// 实测游戏内显示格数与 pos_ 一致（内部 pos 34 = 游戏显示第 34 格），故 POS_BASE = 0；
// 若游戏更新后显示差 1，只需同步修改这里与 Python 侧 board_info 的显示换算。
const POS_BASE = 0;
let gotoTarget = null;
let gotoLevel = null;
let gotoNotice = '';
function readBoard(currentPlayer) {
  const dice = (currentPlayer || player()).add(diceFieldOffset).readPointer();
  if (dice.isNull()) return { ready: false };
  const level = dice.add(diceLevelFieldOffset).readS32();
  const pos = dice.add(dicePosFieldOffset).readS32();
  let gridMax = 0;
  const list = app().add(sugorokuListFieldOffset).readPointer();
  if (!list.isNull() && level >= 0 && level < api.arrayLength(list)) {
    const entry = list.add(16 + level * POINTER).readPointer();
    if (!entry.isNull()) gridMax = entry.add(sugorokuGuridMaxOffset).readS32();
  }
  return { ready: true, level, pos, gridMax };
}
function applyGoto(array) {
  if (array.isNull() || api.arrayLength(array) < 1) return;
  const board = readBoard();
  if (!board.ready) return;
  if (board.level !== gotoLevel) {
    gotoTarget = null;
    gotoNotice = '双六关卡已变化，目标格设置已自动失效';
    send('[修改器] ' + gotoNotice);
    return;
  }
  const steps = gotoTarget - (board.pos + POS_BASE);
  if (steps <= 0) {
    gotoTarget = null;
    gotoNotice = '已到达或越过目标格，目标格设置已自动失效';
    send('[修改器] ' + gotoNotice);
    return;
  }
  const count = api.arrayLength(array);
  if (steps < count) {
    gotoTarget = null;
    gotoNotice = '与目标格的距离小于本次骰子数量，无法精确到达，目标格设置已自动失效';
    send('[修改器] ' + gotoNotice);
    return;
  }
  const walk = Math.min(steps, 99);
  applyStepValues(array, walk, count);
  // 到达后不在此清除：投掷结算、位置变化后，下一次挂钩会由 steps<=0 判定到达并清除。
  // 这样即使游戏在一次投掷前后多次调用 CreateDice（冗余触发）也能稳定命中目标格。
}
function setGotoCell(target) {
  if (!stepLockReady) throw new Error('步数锁定挂钩安装失败，请重新连接游戏');
  const board = readBoard();
  if (!board.ready) throw new Error('双六对局数据尚未初始化，请进入双六屋后刷新');
  if (board.gridMax <= 0) throw new Error('无法读取当前关卡的格子数，请进入双六屋后刷新');
  if (!Number.isInteger(target) || target < 1 || target > board.gridMax) {
    throw new Error('目标格必须在 1 到 ' + board.gridMax + ' 之间');
  }
  const current = board.pos + POS_BASE;
  if (target <= current) {
    throw new Error('目标格必须大于当前所在格数（当前第 ' + current + ' 格）');
  }
  gotoTarget = target;
  gotoLevel = board.level;
  gotoNotice = '';
  return { target, level: board.level, pos: board.pos, gridMax: board.gridMax };
}
function clearGotoCell() {
  gotoTarget = null;
  gotoLevel = null;
  gotoNotice = '';
  return { cleared: true };
}
function readBoardState() {
  return { board: readBoard(), gotoTarget, gotoNotice, stepOverridePending: stepOverride !== null };
}
try {
  const createDice = api.classMethod(subFormClass, Memory.allocUtf8String('CreateDice'), 1);
  if (!createDice.isNull() && !createDice.readPointer().isNull()) {
    Interceptor.attach(createDice.readPointer(), {
      onEnter(args) {
        try {
          if (stepOverride !== null) {
            // 游戏在一次投掷前后可能多次调用 CreateDice。一次性步数必须一直生效到位置真正变化，
            // 否则会被冗余调用提前消耗，表现为“隔一次投掷才生效”。
            let consumed = false;
            try {
              if (stepOverrideApplied) {
                const b = readBoard();
                consumed = b.ready && (b.pos !== stepOverridePos || b.level !== stepOverrideLevel);
              }
            } catch (e) {}
            if (consumed) {
              stepOverride = null;
            } else {
              if (!args[1].isNull() && api.arrayLength(args[1]) > 0) {
                applyStepValues(args[1], stepOverride, api.arrayLength(args[1]));
              }
              try {
                const b = readBoard();
                if (b.ready) {
                  stepOverrideApplied = true;
                  stepOverridePos = b.pos;
                  stepOverrideLevel = b.level;
                }
              } catch (e) {}
              return;
            }
          }
          if (gotoTarget !== null) {
            applyGoto(args[1]);
          } else {
            applyStepLock(args[1]);
          }
        } catch (e) {}
      }
    });
    stepLockReady = true;
  }
} catch (e) {}
function setStepLock(total, count) {
  if (!stepLockReady) throw new Error('步数锁定挂钩安装失败，请重新连接游戏');
  if (!Number.isInteger(count) || count < 1 || count > 3) throw new Error('骰子数量必须在 1 到 3 之间');
  if (!Number.isInteger(total) || total < count || total > 99) {
    throw new Error('总步数必须在 ' + count + ' 到 99 之间');
  }
  stepLockTotal = total;
  stepLockDiceCount = count;
  return {locked: true, total, diceCount: count};
}
function clearStepLock() {
  stepLockTotal = 0;
  stepLockDiceCount = 1;
  return {locked: false};
}
function app() {
  const output = Memory.alloc(POINTER);
  output.writePointer(NULL);
  api.fieldStaticGet(appSingletonField, output);
  const current = output.readPointer();
  if (current.isNull()) throw new Error('游戏尚未初始化，请稍后刷新');
  return current;
}
function player() {
  const current = app().add(playerFieldOffset).readPointer();
  if (current.isNull()) throw new Error('尚未载入存档，请进入正常游戏界面后刷新');
  return current;
}
function invokeInt(klass, object, name) {
  const method = api.classMethod(klass, Memory.allocUtf8String(name), 0);
  if (method.isNull()) throw new Error('找不到方法：' + name);
  const exception = Memory.alloc(POINTER);
  exception.writePointer(NULL);
  const result = api.runtimeInvoke(method, object, NULL, exception);
  if (!exception.readPointer().isNull() || result.isNull()) throw new Error(name + ' 调用失败');
  return result.add(8).readS32();
}
function invokeInt1(klass, object, name, intArg) {
  const method = api.classMethod(klass, Memory.allocUtf8String(name), 1);
  if (method.isNull()) throw new Error('找不到方法：' + name);
  const argMem = Memory.alloc(4);
  argMem.writeS32(intArg);
  const args = Memory.alloc(POINTER);
  args.writePointer(argMem);
  const exception = Memory.alloc(POINTER);
  exception.writePointer(NULL);
  const result = api.runtimeInvoke(method, object, args, exception);
  if (!exception.readPointer().isNull() || result.isNull()) throw new Error(name + ' 调用失败');
  return result.add(8).readS32();
}
function invokeObject1(klass, object, name, intArg) {
  const method = api.classMethod(klass, Memory.allocUtf8String(name), 1);
  if (method.isNull()) throw new Error('找不到方法：' + name);
  const argMem = Memory.alloc(4);
  argMem.writeS32(intArg);
  const args = Memory.alloc(POINTER);
  args.writePointer(argMem);
  const exception = Memory.alloc(POINTER);
  exception.writePointer(NULL);
  const result = api.runtimeInvoke(method, object, args, exception);
  if (!exception.readPointer().isNull() || result.isNull()) throw new Error(name + ' 调用失败');
  return result.add(8).readPointer();
}
function invokeVoid0(klass, object, name) {
  const method = api.classMethod(klass, Memory.allocUtf8String(name), 0);
  if (method.isNull()) throw new Error('找不到方法：' + name);
  const exception = Memory.alloc(POINTER);
  exception.writePointer(NULL);
  api.runtimeInvoke(method, object, NULL, exception);
  if (!exception.readPointer().isNull()) throw new Error(name + ' 调用失败');
}
function setItemNum(item, value) {
  const method = api.classMethod(baseDataClass, Memory.allocUtf8String('SetNum'), 1);
  if (method.isNull()) throw new Error('找不到物品数量写入方法');
  const valuePointer = Memory.alloc(4);
  valuePointer.writeS32(value);
  const args = Memory.alloc(POINTER);
  args.writePointer(valuePointer);
  const exception = Memory.alloc(POINTER);
  exception.writePointer(NULL);
  api.runtimeInvoke(method, item, args, exception);
  if (!exception.readPointer().isNull()) throw new Error('物品数量写入失败');
  if (invokeInt(baseDataClass, item, 'GetNum') !== value) throw new Error('物品数量写入后校验失败');
}
function setExInt(object, value) {
  const method = api.classMethod(exIntClass, Memory.allocUtf8String('Set'), 1);
  if (method.isNull()) throw new Error('找不到数值写入方法');
  const valuePointer = Memory.alloc(4);
  valuePointer.writeS32(value);
  const args = Memory.alloc(POINTER);
  args.writePointer(valuePointer);
  const exception = Memory.alloc(POINTER);
  exception.writePointer(NULL);
  api.runtimeInvoke(method, object, args, exception);
  if (!exception.readPointer().isNull()) throw new Error('数值写入失败');
  if (invokeInt(exIntClass, object, 'Get') !== value) throw new Error('数值写入后校验失败');
}
function exIntAt(object, fieldOffset, label) {
  const wrapper = object.add(fieldOffset).readPointer();
  if (wrapper.isNull()) throw new Error(label + '尚未初始化');
  return wrapper;
}
function readExInt(object, fieldOffset, label) {
  return invokeInt(exIntClass, exIntAt(object, fieldOffset, label), 'Get');
}
function writeInt(object, fieldOffset, value) {
  object.add(fieldOffset).writeS32(value);
  if (object.add(fieldOffset).readS32() !== value) throw new Error('整数写入后校验失败');
}
function staticInt(klass, name) {
  const output = Memory.alloc(4);
  api.fieldStaticGet(getField(klass, name), output);
  return output.readS32();
}
function managedString(object) {
  if (object.isNull()) return '';
  return object.add(12).readUtf16String(object.add(8).readS32());
}
function intArray(object, fieldOffset) {
  const array = object.add(fieldOffset).readPointer();
  if (array.isNull()) throw new Error('属性数组为空');
  const length = api.arrayLength(array);
  if (length < 6) throw new Error('属性数组长度异常：' + length);
  const values = [];
  for (let i = 0; i < 6; i++) values.push(array.add(16 + i * 4).readS32());
  return values;
}
function collection(object) {
  if (object.isNull()) return [];
  let klass = api.objectClass(object);
  const iterator = Memory.alloc(POINTER);
  iterator.writePointer(NULL);
  let listField = NULL;
  let itemsField = NULL;
  let sizeField = NULL;
  let currentClass = klass;
  while (!currentClass.isNull()) {
    const fields = Memory.alloc(POINTER);
    fields.writePointer(NULL);
    for (;;) {
      const field = api.classFields(currentClass, fields);
      if (field.isNull()) break;
      const name = text(api.fieldName(field));
      if (name === 'list_') listField = field;
      if (name === '_items') itemsField = field;
      if (name === '_size') sizeField = field;
    }
    currentClass = api.classParent(currentClass);
  }
  if (!listField.isNull()) return collection(object.add(api.fieldOffset(listField)).readPointer());
  if (itemsField.isNull() || sizeField.isNull()) throw new Error('不支持的集合布局');
  const items = object.add(api.fieldOffset(itemsField)).readPointer();
  const count = object.add(api.fieldOffset(sizeField)).readS32();
  if (count < 0 || count > 10000) throw new Error('集合数量异常：' + count);
  const result = [];
  for (let i = 0; i < count; i++) result.push(items.add(16 + i * POINTER).readPointer());
  return result;
}
function crewObjects(currentPlayer) {
  const result = [];
  const seen = new Set();
  const groups = app().add(humansFieldOffset).readPointer();
  if (groups.isNull()) throw new Error('同伴名单尚未初始化');
  for (let i = 0; i < api.arrayLength(groups); i++) {
    for (const entry of collection(groups.add(16 + i * POINTER).readPointer())) {
      if (entry.isNull()) continue;
      const klass = api.objectClass(entry).toString();
      let crew = entry;
      if (klass === humanClass.toString()) {
        crew = entry.add(offset(humanClass, 'crew_')).readPointer();
      } else if (klass !== characterClass.toString() && klass !== crewClass.toString()) {
        continue;
      }
      if (crew.isNull() || seen.has(crew.toString())) continue;
      seen.add(crew.toString());
      result.push(crew);
    }
  }
  return result;
}
function readCrew(crew, index) {
  return {
    index,
    name: managedString(crew.add(nameOffset).readPointer()),
    level: crew.add(levelOffset).readS32(),
    exp: crew.add(expOffset).readS32(),
    battleHP: crew.add(battleHPOffset).readS32(),
    battleSP: crew.add(battleSPOffset).readS32(),
    params: intArray(crew, paramsOffset),
    addParams: intArray(crew, addParamsOffset)
  };
}
function secure(wrapper) {
  const value = wrapper.add(offset(api.objectClass(wrapper), 'value_')).readPointer();
  if (value.isNull()) throw new Error('数值封装对象为空');
  const klass = api.objectClass(value);
  return {
    object: value,
    originalOffset: offset(klass, 'original_'),
    valueOffset: offset(klass, 'value_'),
    bitmaskOffset: offset(klass, 'bitmask_')
  };
}
function readSecure(wrapper) {
  const secureValue = secure(wrapper);
  return secureValue.object.add(secureValue.originalOffset).readS64().toNumber();
}
function writeSecure(wrapper, value) {
  const secureValue = secure(wrapper);
  const bitmask = secureValue.object.add(secureValue.bitmaskOffset).readS64();
  secureValue.object.add(secureValue.originalOffset).writeS64(new Int64(value));
  secureValue.object.add(secureValue.valueOffset).writeS64(bitmask.xor(new Int64(value)));
  if (readSecure(wrapper) !== value) throw new Error('封装数值写入后校验失败');
}
function researchWrapper(currentPlayer, index) {
  const array = currentPlayer.add(researchFieldOffset).readPointer();
  if (array.isNull() || api.arrayLength(array) <= index) throw new Error('研究点数组异常');
  return array.add(16 + index * POINTER).readPointer();
}
function staminaWrapper(currentPlayer, index) {
  const array = currentPlayer.add(staminaFieldOffset).readPointer();
  if (array.isNull() || api.arrayLength(array) <= index) throw new Error('体力数组异常');
  return array.add(16 + index * POINTER).readPointer();
}
function coinManager() {
  const method = api.classMethod(coinManagerClass, Memory.allocUtf8String('GetInstance'), 0);
  const exception = Memory.alloc(POINTER);
  exception.writePointer(NULL);
  const manager = api.runtimeInvoke(method, NULL, NULL, exception);
  if (!exception.readPointer().isNull() || manager.isNull()) throw new Error('奖牌管理器尚未初始化');
  return manager;
}
function coinNumbers() {
  const manager = coinManager();
  const listOffset = offset(coinManagerClass, 'conList_');
  const numOffset = offset(coinSetClass, 'Num');
  const expirationOffset = offset(coinSetClass, 'Expiration');
  const result = [];
  for (const coinSet of collection(manager.add(listOffset).readPointer())) {
    if (coinSet.add(expirationOffset).readU8() === 0) {
      result.push(coinSet.add(numOffset).readPointer());
    }
  }
  if (!result.length) throw new Error('当前没有可修改的奖牌计数');
  return result;
}
function readCoinNumber(number) {
  return number.add(offset(secureLongClass, 'original_')).readS64().toNumber();
}
function writeCoinNumber(number, value) {
  const bitmask = number.add(offset(secureLongClass, 'bitmask_')).readS64();
  number.add(offset(secureLongClass, 'original_')).writeS64(new Int64(value));
  number.add(offset(secureLongClass, 'value_')).writeS64(bitmask.xor(new Int64(value)));
  if (readCoinNumber(number) !== value) throw new Error('奖牌写入后校验失败');
}
function readMedals() {
  return coinNumbers().reduce((total, number) => total + readCoinNumber(number), 0);
}
function writeMedals(value) {
  const numbers = coinNumbers();
  const newFirstValue = readCoinNumber(numbers[0]) + value - readMedals();
  if (newFirstValue < 0) throw new Error('目标奖牌数低于其它奖牌明细之和，无法安全应用');
  writeCoinNumber(numbers[0], newFirstValue);
  if (readMedals() !== value) throw new Error('奖牌总数写入后校验失败');
}
function itemArrayByTab(tab) {
  const array = app().add(tab === 0 ? equipListFieldOffset : itemListFieldOffset).readPointer();
  if (array.isNull()) throw new Error(tab === 0 ? '武器列表尚未初始化' : '物品列表尚未初始化');
  return array;
}
// 游戏道具分页：0=武器（装备） 1=道具 2=强化（强化用食物）。
// 直接复用游戏自身的 SubForm.GetItemList(tab) 枚举，保证与游戏内分页一致。
function readItemsByTab(tab) {
  const form = api.objectNew(subFormClass);
  if (form.isNull()) throw new Error('无法创建游戏窗口对象');
  invokeVoid0(subFormClass, form, '.ctor');
  const rows = collection(invokeObject1(subFormClass, form, 'GetItemList', tab));
  const result = [];
  for (const row of rows) {
    if (row.isNull() || api.arrayLength(row) < 2) continue;
    const item = row.add(16 + POINTER).readPointer();
    if (item.isNull()) continue;
    const count = invokeInt(baseDataClass, item, 'GetNum');
    if (count <= 0) continue;
    const isEquip = api.objectClass(item).toString() === equipDataClass.toString();
    result.push({
      id: item.add(itemIdOffset).readS32(),
      name: managedString(item.add(isEquip ? equipNameOffset : itemNameOffset).readPointer()),
      count,
      maximum: invokeInt(isEquip ? equipDataClass : itemDataClass, item, 'GetMaxNum')
    });
  }
  return result;
}
function readSugorokuCards() {
  const cards = [];
  const cardArray = app().add(sugorokuItemFieldOffset).readPointer();
  if (cardArray.isNull()) return {cards, ready: false};
  for (let i = 0; i < api.arrayLength(cardArray); i++) {
    const card = cardArray.add(16 + i * POINTER).readPointer();
    if (card.isNull()) continue;
    cards.push({
      index: i,
      name: managedString(card.add(sugorokuItemNameOffset).readPointer()),
      count: invokeInt(baseDataClass, card, 'GetNum'),
      maximum: invokeInt(sugorokuItemClass, card, 'GetMaxNum')
    });
  }
  return {cards, ready: true};
}
function readSugoroku(currentPlayer) {
  const cardInfo = readSugorokuCards();
  const dice = currentPlayer.add(diceFieldOffset).readPointer();
  const dicePow = dice.isNull() ? NULL : dice.add(dicePowFieldOffset).readPointer();
  const powers = [];
  let diceReady = false;
  if (!dicePow.isNull() && api.arrayLength(dicePow) >= 1) {
    diceReady = true;
    for (let i = 0; i < api.arrayLength(dicePow); i++) powers.push(dicePow.add(16 + i * 4).readS32());
  }
  const playNum = exIntAt(currentPlayer, sugorokuPlayNumFieldOffset, '双六次数');
  const kairoWrapper = currentPlayer.add(kairoPointFieldOffset).readPointer();
  let board = { ready: false };
  try {
    board = readBoard(currentPlayer);
  } catch (e) {}
  return {
    cards: cardInfo.cards,
    cardsReady: cardInfo.ready,
    step: diceReady ? powers[0] : 0,
    powers,
    diceReady,
    stepLocked: stepLockTotal > 0,
    stepLockTotal,
    stepLockDiceCount,
    board,
    remaining: invokeInt(exIntClass, playNum, 'Get'),
    maximum: staticInt(playerClass, 'SUGOROKU_PLAY_NUM'),
    kairoPoint: kairoWrapper.isNull() ? 0 : invokeInt(exIntClass, kairoWrapper, 'Get'),
    kairoReady: !kairoWrapper.isNull()
  };
}
function readState() {
  const currentPlayer = player();
  return {
    money: readSecure(currentPlayer.add(moneyFieldOffset).readPointer()),
    food: readSecure(researchWrapper(currentPlayer, 0)),
    medals: readMedals(),
    stamina: readSecure(staminaWrapper(currentPlayer, 2)),
    crews: crewObjects(currentPlayer).map(readCrew),
    itemsByTab: [readItemsByTab(0), readItemsByTab(1), readItemsByTab(2)],
    sugoroku: readSugoroku(currentPlayer)
  };
}
function writeIntArray(object, fieldOffset, values) {
  const array = object.add(fieldOffset).readPointer();
  if (array.isNull() || api.arrayLength(array) < 6) throw new Error('属性数组异常');
  for (let i = 0; i < 6; i++) {
    array.add(16 + i * 4).writeS32(values[i]);
    if (array.add(16 + i * 4).readS32() !== values[i]) throw new Error('属性写入后校验失败');
  }
}
function applyChanges(changes) {
  const currentPlayer = player();
  const crews = crewObjects(currentPlayer);
  const PARAM_NAME_TABLE = ['HP', 'SP', '攻击', '防御', '速度', '智慧'];
  const PARAM_SAFE_TOTAL = 9990000;
  const PARAM_SAFE_LIMITED = ['HP', 'SP', '攻击', '防御', '速度'];
  for (const change of changes.crews) {
    if (change.index < 0 || change.index >= crews.length) throw new Error('船员列表已变化，请刷新后重试');
    for (let i = 0; i < 6; i++) {
      const sum = change.params[i] + change.addParams[i];
      const limit = PARAM_SAFE_LIMITED.includes(PARAM_NAME_TABLE[i]) ? PARAM_SAFE_TOTAL : 2000000000;
      if (sum > limit) {
        throw new Error(PARAM_NAME_TABLE[i] + ' 的基础 ' + change.params[i] + ' 与成长 ' + change.addParams[i] +
          ' 合计 ' + sum + ' 超过 ' + limit +
          (limit === PARAM_SAFE_TOTAL
            ? '；该项在探索/战斗计算中含 32 位乘法（属性相性或技能百分比），超出后会导致打不动怪、被秒杀或恢复异常'
            : '；游戏按 32 位整数求和，溢出后该属性会归为 1') +
          '，请调低后再应用');
      }
    }
  }
  writeSecure(currentPlayer.add(moneyFieldOffset).readPointer(), changes.money);
  writeSecure(researchWrapper(currentPlayer, 0), changes.food);
  writeMedals(changes.medals);
  writeSecure(staminaWrapper(currentPlayer, 2), changes.stamina);
  for (const change of changes.crews) {
    const crew = crews[change.index];
    writeInt(crew, levelOffset, change.level);
    writeInt(crew, expOffset, change.exp);
    writeIntArray(crew, paramsOffset, change.params);
    writeIntArray(crew, addParamsOffset, change.addParams);
    let battleHP = change.battleHP;
    let battleSP = change.battleSP;
    if (change.restoreBattle) {
      battleHP = invokeInt1(characterClass, crew, 'GetParam', 0);
      battleSP = invokeInt1(characterClass, crew, 'GetParam', 1);
    }
    writeInt(crew, battleHPOffset, battleHP);
    writeInt(crew, battleSPOffset, battleSP);
  }
  return readState();
}
function applyItemTabChanges(changes) {
  const array = itemArrayByTab(changes.tab);
  const length = api.arrayLength(array);
  const wanted = new Set(changes.ids);
  const plans = [];
  for (let i = 0; i < length; i++) {
    const item = array.add(16 + i * POINTER).readPointer();
    if (item.isNull() || !wanted.has(item.add(itemIdOffset).readS32())) continue;
    if (invokeInt(baseDataClass, item, 'GetNum') <= 0) throw new Error('物品已不再持有，请刷新后重试');
    const isEquip = api.objectClass(item).toString() === equipDataClass.toString();
    const maximum = invokeInt(isEquip ? equipDataClass : itemDataClass, item, 'GetMaxNum');
    const value = changes.safeMax ? Math.max(0, maximum - 9) : changes.value;
    if (!Number.isInteger(value) || value < 0 || value > maximum) {
      throw new Error('物品数量必须在 0 到当前上限 ' + maximum + ' 之间');
    }
    plans.push({item, value});
  }
  if (plans.length !== wanted.size) throw new Error('物品列表已变化，请刷新后重试');
  for (const plan of plans) setItemNum(plan.item, plan.value);
  return readState();
}
function applySugorokuChanges(changes) {
  if (Array.isArray(changes.cards) && changes.cards.length) {
    const array = app().add(sugorokuItemFieldOffset).readPointer();
    if (array.isNull()) throw new Error('双六卡片列表尚未初始化，请进入双六屋后刷新');
    const length = api.arrayLength(array);
    const plans = [];
    for (const index of new Set(changes.cards)) {
      if (!Number.isInteger(index) || index < 0 || index >= length) throw new Error('双六卡片列表已变化，请刷新后重试');
      const card = array.add(16 + index * POINTER).readPointer();
      if (card.isNull()) throw new Error('双六卡片不存在，请刷新后重试');
      const maximum = invokeInt(sugorokuItemClass, card, 'GetMaxNum');
      const value = changes.safeMax ? (maximum > 1 ? maximum - 1 : maximum) : changes.cardValue;
      if (!Number.isInteger(value) || value < 0 || value > maximum) {
        throw new Error('卡片数量必须在 0 到当前上限 ' + maximum + ' 之间');
      }
      plans.push({card, value});
    }
    for (const plan of plans) setItemNum(plan.card, plan.value);
  }
  const currentPlayer = player();
  if (changes.step !== undefined) {
    const diceCount = changes.diceCount === undefined ? 1 : changes.diceCount;
    if (!Number.isInteger(diceCount) || diceCount < 1 || diceCount > 3) throw new Error('骰子数量必须在 1 到 3 之间');
    if (!Number.isInteger(changes.step) || changes.step < diceCount || changes.step > 99) {
      throw new Error('总步数必须在 ' + diceCount + ' 到 99 之间');
    }
    const dice = currentPlayer.add(diceFieldOffset).readPointer();
    const dicePow = dice.isNull() ? NULL : dice.add(dicePowFieldOffset).readPointer();
    if (dicePow.isNull() || api.arrayLength(dicePow) < diceCount) throw new Error('骰子数据尚未初始化');
    const base = Math.floor(changes.step / diceCount);
    const extra = changes.step % diceCount;
    for (let i = 0; i < diceCount; i++) writeInt(dicePow, 16 + i * 4, base + (i < extra ? 1 : 0));
    stepOverride = changes.step;
    stepOverrideApplied = false;
    stepOverridePos = -1;
    stepOverrideLevel = -1;
  }
  if (changes.remaining !== undefined) {
    const maximum = staticInt(playerClass, 'SUGOROKU_PLAY_NUM');
    if (!Number.isInteger(changes.remaining) || changes.remaining < 0 || changes.remaining > maximum) {
      throw new Error('剩余投掷次数必须在 0 到 ' + maximum + ' 之间');
    }
    const playNum = currentPlayer.add(sugorokuPlayNumFieldOffset).readPointer();
    if (playNum.isNull()) throw new Error('双六次数尚未初始化');
    setExInt(playNum, changes.remaining);
  }
  if (changes.kairoPoint !== undefined) {
    if (!Number.isInteger(changes.kairoPoint) || changes.kairoPoint < 0 || changes.kairoPoint > 2147483647) {
      throw new Error('开罗点数必须在 0 到 2147483647 之间');
    }
    setExInt(exIntAt(currentPlayer, kairoPointFieldOffset, '开罗点数'), changes.kairoPoint);
  }
  return readState();
}
function readSugorokuRemaining() {
  const currentPlayer = player();
  return {
    remaining: readExInt(currentPlayer, sugorokuPlayNumFieldOffset, '双六次数'),
    maximum: staticInt(playerClass, 'SUGOROKU_PLAY_NUM')
  };
}
function writeSugorokuRemaining(value) {
  const maximum = staticInt(playerClass, 'SUGOROKU_PLAY_NUM');
  if (!Number.isInteger(value) || value < 0 || value > maximum) {
    throw new Error('剩余投掷次数必须在 0 到 ' + maximum + ' 之间');
  }
  const wrapper = exIntAt(player(), sugorokuPlayNumFieldOffset, '双六次数');
  setExInt(wrapper, value);
  return {remaining: invokeInt(exIntClass, wrapper, 'Get'), maximum};
}
rpc.exports = {
  readState,
  applyChanges,
  applyItemTabChanges,
  applySugorokuChanges,
  readSugorokuRemaining,
  writeSugorokuRemaining,
  setStepLock,
  clearStepLock,
  setGotoCell,
  clearGotoCell,
  readBoardState
};
"""


class Tooltip:
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.window = None
        widget.bind("<Enter>", self.show)
        widget.bind("<Leave>", self.hide)
        widget.bind("<FocusIn>", self.show)
        widget.bind("<FocusOut>", self.hide)

    def show(self, _event=None):
        if self.window is not None:
            return
        self.window = tk.Toplevel(self.widget)
        self.window.wm_overrideredirect(True)
        self.window.wm_attributes("-topmost", True)
        x = self.widget.winfo_rootx() + self.widget.winfo_width() + 8
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.window.wm_geometry(f"+{x}+{y}")
        tk.Label(
            self.window,
            text=self.text,
            justify="left",
            wraplength=420,
            background="#fff8dc",
            relief="solid",
            borderwidth=1,
            padx=9,
            pady=7,
        ).pack()

    def hide(self, _event=None):
        if self.window is not None:
            self.window.destroy()
            self.window = None


class IL2CPPGame:
    def __init__(self):
        self.session = None
        self.script = None
        self.pid = None
        self.backup_created = False

    def connect(self):
        self.close()
        device = frida.get_local_device()
        process = next((p for p in device.enumerate_processes() if p.name.lower() == GAME_NAME.lower()), None)
        if process is None:
            raise RuntimeError("未找到 KairoGames.exe，请先启动游戏并载入存档")
        self.session = device.attach(process.pid)
        try:
            self.script = self.session.create_script(IL2CPP_SCRIPT)
            self.script.load()
            self.pid = process.pid
        except Exception:
            self.close()
            raise

    def read_state(self):
        if self.script is None:
            raise RuntimeError("请先连接游戏")
        return self.script.exports_sync.read_state()

    def apply_changes(self, changes):
        if self.script is None:
            raise RuntimeError("请先连接游戏")
        if not self.backup_created:
            self.backup_saves()
        result = self.script.exports_sync.apply_changes(changes)
        self.backup_created = True
        return result

    def apply_tab_items(self, tab, ids, value=None, safe_max=False):
        if self.script is None:
            raise RuntimeError("请先连接游戏")
        if not self.backup_created:
            self.backup_saves()
        result = self.script.exports_sync.apply_item_tab_changes({
            "tab": tab,
            "ids": ids,
            "value": value,
            "safeMax": safe_max,
        })
        self.backup_created = True
        return result

    def apply_sugoroku(self, changes):
        if self.script is None:
            raise RuntimeError("请先连接游戏")
        if not self.backup_created:
            self.backup_saves()
            self.backup_created = True
        return self.script.exports_sync.apply_sugoroku_changes(changes)

    def apply_cards(self, indices, value=None, safe_max=False):
        if self.script is None:
            raise RuntimeError("请先连接游戏")
        if not self.backup_created:
            self.backup_saves()
            self.backup_created = True
        return self.script.exports_sync.apply_sugoroku_changes({
            "cards": indices,
            "cardValue": value,
            "safeMax": safe_max,
        })

    def read_sugoroku_remaining(self):
        if self.script is None:
            raise RuntimeError("请先连接游戏")
        return self.script.exports_sync.read_sugoroku_remaining()

    def write_sugoroku_remaining(self, value):
        if self.script is None:
            raise RuntimeError("请先连接游戏")
        if not self.backup_created:
            self.backup_saves()
            self.backup_created = True
        return self.script.exports_sync.write_sugoroku_remaining(value)

    def set_step_lock(self, total, count):
        if self.script is None:
            raise RuntimeError("请先连接游戏")
        return self.script.exports_sync.set_step_lock(total, count)

    def clear_step_lock(self):
        if self.script is None:
            raise RuntimeError("请先连接游戏")
        return self.script.exports_sync.clear_step_lock()

    def set_goto_cell(self, target):
        if self.script is None:
            raise RuntimeError("请先连接游戏")
        return self.script.exports_sync.set_goto_cell(target)

    def clear_goto_cell(self):
        if self.script is None:
            raise RuntimeError("请先连接游戏")
        return self.script.exports_sync.clear_goto_cell()

    def read_board(self):
        if self.script is None:
            raise RuntimeError("请先连接游戏")
        return self.script.exports_sync.read_board_state()

    def ensure_backup(self):
        if not self.backup_created:
            self.backup_saves()
            self.backup_created = True

    def backup_saves(self):
        if not GAME_SAVES.is_dir():
            raise RuntimeError(f"找不到存档目录：{GAME_SAVES}")
        destination = BACKUP_ROOT / datetime.now().strftime("%Y%m%d-%H%M%S-%f") / "saves"
        destination.parent.mkdir(parents=True, exist_ok=False)
        shutil.copytree(GAME_SAVES, destination)

    def close(self):
        if self.script is not None:
            try:
                self.script.unload()
            except Exception:
                pass
        if self.session is not None:
            try:
                self.session.detach()
            except Exception:
                pass
        self.script = None
        self.session = None
        self.pid = None


class TrainerApp(tk.Tk):
    def __init__(self):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except (AttributeError, OSError):
                pass
        super().__init__()
        dpi_scale = self.winfo_fpixels("1i") / 96
        self.title("High Sea Saga DX 直接属性修改器")
        self.geometry(f"{round(760 * dpi_scale)}x{round(680 * dpi_scale)}")
        self.minsize(round(640 * dpi_scale), round(600 * dpi_scale))
        self.game = IL2CPPGame()
        self.state = None
        self.selected_crew = 0
        self.process_var = tk.StringVar(value="未连接")
        self.status_var = tk.StringVar(value="正在准备连接游戏……")
        self.summary_var = tk.StringVar(value="尚未读取数据")
        self.resource_vars = {name: tk.StringVar() for name, _label in RESOURCE_FIELDS}
        self.crew_choice_var = tk.StringVar()
        self.crew_vars = {
            name: tk.StringVar()
            for name in ("level", "exp", "battleHP", "battleSP")
        }
        self.param_vars = [tk.StringVar() for _ in PARAM_NAMES]
        self.add_param_vars = [tk.StringVar() for _ in PARAM_NAMES]
        self.crew_headroom_var = tk.StringVar(value="请先刷新读取数据")
        self.item_tab_names = ("武器", "道具", "强化")
        self.item_tabs = {
            tab: {
                "checked": set(),
                "value_var": tk.StringVar(),
                "limit_var": tk.StringVar(value="请勾选要修改的物品"),
            }
            for tab in range(len(self.item_tab_names))
        }
        self.checked_cards = set()
        self.card_value_var = tk.StringVar()
        self.card_limit_var = tk.StringVar(value="请勾选要修改的卡片")
        self.dice_count_var = tk.StringVar(value="1")
        self.dice_step_var = tk.StringVar()
        self.dice_remaining_var = tk.StringVar()
        self.board_info_var = tk.StringVar(value="尚未读取棋盘状态，请刷新")
        self.goto_var = tk.StringVar(value="1")
        self.goto_status_var = tk.StringVar(value="设置后，下一次投掷会按实时位置自动计算步数走过去")
        self.kairo_point_var = tk.StringVar()
        self.kairo_point_limit_var = tk.StringVar(value="请先刷新读取数据")
        self.lock_remaining_var = tk.BooleanVar(value=False)
        self.lock_step_var = tk.BooleanVar(value=False)
        self._lock_timer_id = None
        self._lock_target_value = 0
        self._lock_failures = 0
        self._column_save_timer_id = None
        self.settings = self.load_settings()
        self.tooltips = []
        self._setup_style()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self.connect)

    def add_help(self, parent, text):
        label = ttk.Label(parent, text="？", foreground="#1769aa", cursor="question_arrow", takefocus=1)
        self.tooltips.append(Tooltip(label, text))
        return label

    def _setup_style(self):
        available = set(tkfont.families())
        family = next(
            (name for name in ("LXGW WenKai", "霞鹜文楷", "Microsoft YaHei UI") if name in available),
            "TkDefaultFont",
        )
        self.font_base = tkfont.Font(family=family, size=9)
        self.font_heading = tkfont.Font(family=family, size=10, weight="bold")
        self.font_big = tkfont.Font(family=family, size=14)
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            tkfont.nametofont(name).configure(family=family, size=9)
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", font=self.font_base)
        style.configure("TFrame", background="#f5f6f8")
        style.configure("TLabel", background="#f5f6f8")
        style.configure("TCheckbutton", background="#f5f6f8")
        style.configure("TLabelframe", background="#f5f6f8")
        style.configure("TLabelframe.Label", background="#f5f6f8", font=self.font_heading, foreground="#1769aa")
        style.configure("TNotebook", background="#f5f6f8")
        style.configure("TNotebook.Tab", font=self.font_heading, padding=(16, 6))
        style.configure("Treeview", font=self.font_base, rowheight=24)
        style.configure("Treeview.Heading", font=self.font_heading)
        style.configure(
            "Accent.TButton",
            font=self.font_heading,
            foreground="#ffffff",
            background="#1769aa",
            padding=(12, 4),
        )
        style.map(
            "Accent.TButton",
            background=[("active", "#0d47a1"), ("disabled", "#9bb8d3")],
            foreground=[("disabled", "#e8edf2")],
        )
        style.configure("Heading.TLabel", font=self.font_heading, foreground="#1769aa")
        style.configure("Hint.TLabel", foreground="#6b7280")
        style.configure("Big.TLabel", font=self.font_big, foreground="#1769aa")
        style.configure("Status.Info.TLabel", foreground="#1769aa", background="#e8edf2")
        style.configure("Status.Ok.TLabel", foreground="#2e7d32", background="#e6f0e6")
        style.configure("Status.Error.TLabel", foreground="#c62828", background="#f7e6e6")

    def _recolor_status(self, *_args):
        text = self.status_var.get()
        if "失败" in text or "无法" in text:
            level = "Error"
        elif "成功" in text or text.startswith("已"):
            level = "Ok"
        else:
            level = "Info"
        self.status_label.configure(style=f"Status.{level}.TLabel")

    def _build_ui(self):
        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)

        self.notebook = ttk.Notebook(root)
        self.notebook.grid(row=0, column=0, sticky="nsew", pady=(0, 8))
        overview_page = ttk.Frame(self.notebook, padding=12)
        item_page = ttk.Frame(self.notebook, padding=12)
        sugoroku_page = ttk.Frame(self.notebook, padding=12)
        crew_page = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(overview_page, text="概览")
        self.notebook.add(item_page, text="物品修改")
        self.notebook.add(sugoroku_page, text="双六屋")
        self.notebook.add(crew_page, text="船员属性")

        overview_page.columnconfigure(0, weight=1)

        connection = ttk.LabelFrame(overview_page, text="游戏连接", padding=10)
        connection.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Button(connection, text="重新连接", style="Accent.TButton", command=self.connect).pack(side="left")
        ttk.Button(connection, text="刷新数值", command=self.refresh).pack(side="left", padx=8)
        ttk.Label(connection, textvariable=self.process_var, style="Heading.TLabel").pack(side="left", padx=8)

        resources = ttk.LabelFrame(overview_page, text="基础资源数据", padding=12)
        resources.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        resources.columnconfigure(1, weight=1)
        for row, (key, label) in enumerate(RESOURCE_FIELDS):
            ttk.Label(resources, text=label).grid(row=row, column=0, padx=(0, 12), pady=5, sticky="w")
            ttk.Entry(resources, textvariable=self.resource_vars[key], width=22).grid(row=row, column=1, pady=5, sticky="ew")
        ttk.Label(resources, text="体力修改的是当前体力，不修改体力上限", style="Hint.TLabel").grid(
            row=len(RESOURCE_FIELDS), column=0, columnspan=2, pady=(8, 0), sticky="w"
        )
        resource_actions = ttk.Frame(resources)
        resource_actions.grid(row=len(RESOURCE_FIELDS) + 1, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Button(resource_actions, text="应用资源修改", style="Accent.TButton", command=self.apply_resources).pack(side="left")
        ttk.Label(resource_actions, text="仅修改本页四项基础资源；首次写入前自动备份存档", style="Hint.TLabel").pack(side="left", padx=10)

        summary = ttk.LabelFrame(overview_page, text="存档概况", padding=12)
        summary.grid(row=2, column=0, sticky="ew")
        ttk.Label(summary, textvariable=self.summary_var, style="Big.TLabel").pack(anchor="w")
        ttk.Label(summary, text=f"存档备份目录：{BACKUP_ROOT}", style="Hint.TLabel").pack(anchor="w", pady=(6, 0))

        item_page.columnconfigure(0, weight=1)
        item_page.rowconfigure(0, weight=1)
        item_notebook = ttk.Notebook(item_page)
        item_notebook.grid(row=0, column=0, sticky="nsew")
        for tab, tab_name in enumerate(self.item_tab_names):
            self._build_item_tab(item_notebook, tab, tab_name)

        sugoroku_page.columnconfigure(0, weight=1)
        sugoroku_page.rowconfigure(0, weight=1)
        sugoroku_notebook = ttk.Notebook(sugoroku_page)
        sugoroku_notebook.grid(row=0, column=0, sticky="nsew")
        dice_tab = ttk.Frame(sugoroku_notebook, padding=12)
        card_tab = ttk.Frame(sugoroku_notebook, padding=12)
        sugoroku_notebook.add(dice_tab, text="投掷")
        sugoroku_notebook.add(card_tab, text="卡片")

        dice_tab.columnconfigure(0, weight=1)
        dice_editor = ttk.LabelFrame(dice_tab, text="投掷设置", padding=12)
        dice_editor.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(dice_editor, text="剩余投掷次数").grid(row=0, column=0, sticky="w", pady=4)
        self.dice_remaining_spinbox = ttk.Spinbox(
            dice_editor, from_=0, to=3, textvariable=self.dice_remaining_var, width=6
        )
        self.dice_remaining_spinbox.grid(row=0, column=1, padx=(6, 8), pady=4, sticky="w")
        self.dice_remaining_spinbox.bind("<Return>", lambda _event: self.apply_dice_remaining())
        ttk.Button(dice_editor, text="应用次数", command=self.apply_dice_remaining).grid(row=0, column=2, padx=(0, 12), pady=4)
        ttk.Checkbutton(
            dice_editor,
            text="锁定剩余次数",
            variable=self.lock_remaining_var,
            command=self.toggle_lock_remaining,
        ).grid(row=0, column=3, sticky="w", pady=4)

        ttk.Label(dice_editor, text="骰子数量").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Combobox(dice_editor, textvariable=self.dice_count_var, values=(1, 2, 3), state="readonly", width=5).grid(row=1, column=1, padx=(6, 8), pady=4, sticky="w")

        ttk.Label(dice_editor, text="总步数").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Spinbox(dice_editor, from_=1, to=99, textvariable=self.dice_step_var, width=6).grid(row=2, column=1, padx=(6, 8), pady=4, sticky="w")
        ttk.Button(dice_editor, text="应用步数", command=self.apply_dice_step).grid(row=2, column=2, padx=(0, 12), pady=4)
        step_lock = ttk.Frame(dice_editor)
        step_lock.grid(row=2, column=3, sticky="w", pady=4)
        ttk.Checkbutton(
            step_lock,
            text="锁定步数",
            variable=self.lock_step_var,
            command=self.toggle_lock_step,
        ).pack(side="left")
        self.add_help(step_lock, "勾选后每次投掷都强制使用设定的总步数；不勾选则只对下一次投掷生效一次。99 是修改器的安全上限，不是游戏硬上限。").pack(side="left", padx=(4, 0))
        ttk.Label(dice_editor, text="总步数允许 1–99；多骰时不得小于骰子数量；应用后下一次投掷强制生效", style="Hint.TLabel").grid(row=3, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Label(dice_editor, textvariable=self.board_info_var, style="Hint.TLabel").grid(row=4, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Button(dice_editor, text="刷新位置", command=self.refresh_board).grid(row=4, column=3, sticky="w", pady=(10, 0))

        ttk.Label(dice_editor, text="目标格").grid(row=5, column=0, sticky="w", pady=4)
        self.goto_spinbox = ttk.Spinbox(dice_editor, from_=1, to=200, textvariable=self.goto_var, width=6)
        self.goto_spinbox.grid(row=5, column=1, padx=(6, 8), pady=4, sticky="w")
        ttk.Button(dice_editor, text="走到目标格", command=self.apply_goto).grid(row=5, column=2, padx=(0, 12), pady=4)
        goto_tools = ttk.Frame(dice_editor)
        goto_tools.grid(row=5, column=3, sticky="w", pady=4)
        ttk.Button(goto_tools, text="取消目标", command=self.clear_goto).pack(side="left")
        self.add_help(
            goto_tools,
            "设置目标格后，每次投掷前会读取人物当前位置，按“目标格 - 当前格”计算步数并写入骰子；"
            "超过 99 步会自动保留设置、分多次投掷走过去；到达、越过目标格或切换关卡后自动失效。"
            "与“锁定步数”同时存在时优先走目标格；点过“应用步数”的单次步数优先于目标格。",
        ).pack(side="left", padx=(4, 0))
        ttk.Label(dice_editor, textvariable=self.goto_status_var, style="Hint.TLabel").grid(row=6, column=0, columnspan=4, sticky="w", pady=(8, 0))

        kairo_editor = ttk.LabelFrame(dice_tab, text="开罗点数", padding=12)
        kairo_editor.grid(row=1, column=0, sticky="ew")
        ttk.Label(kairo_editor, text="开罗点数").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(kairo_editor, textvariable=self.kairo_point_var, width=14).grid(row=0, column=1, padx=8, pady=4, sticky="w")
        ttk.Button(kairo_editor, text="应用开罗点数", style="Accent.TButton", command=self.apply_kairo_point).grid(row=0, column=2, pady=4)
        ttk.Label(kairo_editor, textvariable=self.kairo_point_limit_var, style="Hint.TLabel").grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))

        card_tab.columnconfigure(0, weight=1)
        card_tab.rowconfigure(1, weight=1)
        card_tools = ttk.Frame(card_tab)
        card_tools.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(card_tools, text="扫描卡片", command=self.refresh).pack(side="left")
        ttk.Button(card_tools, text="全部勾选", command=self.select_all_cards).pack(side="left", padx=8)
        ttk.Button(card_tools, text="清空勾选", command=self.clear_card_selection).pack(side="left")

        card_list = ttk.Frame(card_tab)
        card_list.grid(row=1, column=0, sticky="nsew")
        card_list.columnconfigure(0, weight=1)
        card_list.rowconfigure(0, weight=1)
        self.card_tree = ttk.Treeview(
            card_list,
            columns=("checked", "name", "count", "maximum"),
            show="headings",
            selectmode="browse",
            height=10,
        )
        self.card_tree.heading("checked", text="选择")
        self.card_tree.heading("name", text="卡片名称")
        self.card_tree.heading("count", text="当前数量")
        self.card_tree.heading("maximum", text="持有上限")
        self.card_tree.column("checked", width=50, minwidth=40, stretch=False, anchor="center")
        self.card_tree.column("name", width=280, minwidth=160, stretch=False, anchor="w")
        self.card_tree.column("count", width=100, minwidth=80, stretch=False, anchor="center")
        self.card_tree.column("maximum", width=110, minwidth=90, stretch=False, anchor="center")
        self.restore_column_widths()
        self.card_tree.grid(row=0, column=0, sticky="nsew")
        card_scroll = ttk.Scrollbar(card_list, orient="vertical", command=self.card_tree.yview)
        card_scroll.grid(row=0, column=1, sticky="ns")
        card_scroll_x = ttk.Scrollbar(card_list, orient="horizontal", command=self.card_tree.xview)
        card_scroll_x.grid(row=1, column=0, sticky="ew")
        self.card_tree.configure(yscrollcommand=card_scroll.set, xscrollcommand=card_scroll_x.set)
        self.card_tree.bind("<<TreeviewSelect>>", self.on_card_selected)
        self.card_tree.bind("<Button-1>", self.toggle_card_check)
        self.card_tree.bind("<ButtonRelease-1>", self.schedule_save_column_widths)

        card_editor = ttk.LabelFrame(card_tab, text="批量修改勾选卡片", padding=10)
        card_editor.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        ttk.Label(card_editor, text="目标数量").pack(side="left")
        ttk.Entry(card_editor, textvariable=self.card_value_var, width=12).pack(side="left", padx=8)
        ttk.Button(card_editor, text="修改数量并应用", style="Accent.TButton", command=self.apply_card_value).pack(side="left", padx=(0, 12))
        ttk.Button(card_editor, text="一键设为上限 - 1", command=self.apply_card_safe_max).pack(side="left", padx=(0, 4))
        self.add_help(card_editor, "将勾选卡片分别设为各自的实时上限减 1（上限为 1 时直接设为 1），留出余量防止游戏内继续获得时溢出。不同卡片的上限独立计算。").pack(side="left")
        ttk.Label(card_tab, textvariable=self.card_limit_var, style="Hint.TLabel").grid(row=3, column=0, sticky="w", pady=(8, 0))

        crew_page.columnconfigure(0, weight=1)
        selector = ttk.Frame(crew_page)
        selector.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(selector, text="当前船员").pack(side="left")
        self.crew_selector = ttk.Combobox(selector, textvariable=self.crew_choice_var, state="readonly", width=40)
        self.crew_selector.pack(side="left", padx=10)
        self.crew_selector.bind("<<ComboboxSelected>>", self.on_crew_selected)

        basic = ttk.LabelFrame(crew_page, text="船员成长与战斗数值", padding=10)
        basic.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        basic.columnconfigure(1, weight=1)
        basic.columnconfigure(3, weight=1)
        basic.columnconfigure(5, weight=1)
        basic.columnconfigure(7, weight=1)
        for column, (key, label) in enumerate((("level", "等级"), ("exp", "经验"), ("battleHP", "当前战斗 HP"), ("battleSP", "当前战斗 SP"))):
            ttk.Label(basic, text=label).grid(row=0, column=column * 2, padx=(0, 5), sticky="w")
            ttk.Entry(basic, textvariable=self.crew_vars[key], width=10).grid(row=0, column=column * 2 + 1, padx=(0, 12), sticky="ew")
        ttk.Button(basic, text="战斗 HP/SP 恢复满", command=self.apply_crew_restore).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.add_help(
            basic,
            "按游戏当前计算出的 HP/SP 上限（基础+额外成长+职业成长+装备+技能+同伴/船只全部加成后的生效值），"
            "把当前船员的战斗 HP、战斗 SP 写满。只恢复战斗数值，不修改等级、经验和六项属性。",
        ).grid(row=1, column=2, sticky="w", padx=(6, 0), pady=(8, 0))

        params = ttk.LabelFrame(crew_page, text="六项基础属性（param_）", padding=10)
        params.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        params.columnconfigure(1, weight=1)
        params.columnconfigure(3, weight=1)
        params.columnconfigure(5, weight=1)
        self._param_entries(params, self.param_vars)

        additions = ttk.LabelFrame(crew_page, text="六项额外成长（addParam_）", padding=10)
        additions.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        additions.columnconfigure(1, weight=1)
        additions.columnconfigure(3, weight=1)
        additions.columnconfigure(5, weight=1)
        self._param_entries(additions, self.add_param_vars)
        for var in self.param_vars + self.add_param_vars:
            var.trace_add("write", lambda *_args: self.update_crew_headroom())

        crew_actions = ttk.Frame(crew_page)
        crew_actions.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        ttk.Button(crew_actions, text="应用船员修改", style="Accent.TButton", command=self.apply_crew).pack(side="left")
        ttk.Label(crew_actions, text="仅修改当前选中的 1 名船员；首次写入前自动备份存档", style="Hint.TLabel").pack(side="left", padx=10)
        ttk.Button(crew_actions, text="一键拉满上限", command=self.apply_crew_max).pack(side="left", padx=(14, 0))
        self.add_help(
            crew_actions,
            "一键将当前船员六项基础设为 10000、额外成长设为 9980000（HP/SP/攻/防/速 合计 9990000，"
            "智慧同值）；该值为探索/战斗安全上限——攻/防/速在战斗中会乘属性相性、HP/SP/速度会被技能"
            "百分比放大，两处都是 32 位乘法，合计超过约 1000 万就会溢出，导致打不动怪、被秒杀或恢复异常，"
            "探索直接失败。生效数值约 1000 万，已足够秒杀一切，且会显著降低游戏乐趣，请确认后再使用；"
            "首次写入前会自动备份存档。",
        ).pack(side="left", padx=(4, 0))
        self.crew_headroom_label = ttk.Label(crew_page, textvariable=self.crew_headroom_var, style="Hint.TLabel")
        self.crew_headroom_label.grid(row=5, column=0, sticky="w", pady=(8, 0))
        ttk.Label(
            crew_page,
            text="同一属性 基础 + 成长 合计：HP/SP/攻/防/速 不要超过 9990000（战斗相性/技能百分比含 32 位乘法，"
            "超出会探索失败），智慧不要超过 2000000000（求和溢出归为 1）",
            style="Hint.TLabel",
        ).grid(row=6, column=0, sticky="w", pady=(4, 0))

        self.status_label = ttk.Label(root, textvariable=self.status_var, style="Status.Info.TLabel", relief="sunken", anchor="w", padding=6)
        self.status_label.grid(row=1, column=0, sticky="ew")
        self.status_var.trace_add("write", self._recolor_status)

    def _build_item_tab(self, notebook, tab, tab_name):
        page = ttk.Frame(notebook, padding=12)
        notebook.add(page, text=tab_name)
        page.columnconfigure(0, weight=1)
        page.rowconfigure(1, weight=1)
        info = self.item_tabs[tab]

        tools = ttk.Frame(page)
        tools.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(tools, text="扫描背包", command=self.refresh).pack(side="left")
        ttk.Button(tools, text="全部勾选", command=lambda: self.select_all_items(tab)).pack(side="left", padx=8)
        ttk.Button(tools, text="清空勾选", command=lambda: self.clear_item_selection(tab)).pack(side="left")

        list_frame = ttk.Frame(page)
        list_frame.grid(row=1, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        tree = ttk.Treeview(
            list_frame,
            columns=("checked", "name", "count", "maximum"),
            show="headings",
            selectmode="browse",
        )
        tree.heading("checked", text="选择")
        tree.heading("name", text="物品名称")
        tree.heading("count", text="当前数量")
        tree.heading("maximum", text="当前持有上限")
        tree.column("checked", width=50, minwidth=40, stretch=False, anchor="center")
        tree.column("name", width=260, minwidth=160, stretch=False, anchor="w")
        tree.column("count", width=100, minwidth=80, stretch=False, anchor="center")
        tree.column("maximum", width=110, minwidth=90, stretch=False, anchor="center")
        tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        scroll_x = ttk.Scrollbar(list_frame, orient="horizontal", command=tree.xview)
        scroll_x.grid(row=1, column=0, sticky="ew")
        tree.configure(yscrollcommand=scroll.set, xscrollcommand=scroll_x.set)
        tree.bind("<<TreeviewSelect>>", lambda _event, t=tab: self.on_items_selected(t))
        tree.bind("<Button-1>", lambda event, t=tab: self.toggle_item_check(t, event))
        tree.bind("<ButtonRelease-1>", self.schedule_save_column_widths)
        info["tree"] = tree

        editor = ttk.LabelFrame(page, text=f"批量修改勾选{tab_name}", padding=10)
        editor.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        ttk.Label(editor, text="目标数量").pack(side="left")
        ttk.Entry(editor, textvariable=info["value_var"], width=12).pack(side="left", padx=8)
        ttk.Button(editor, text="修改数值并应用", style="Accent.TButton", command=lambda: self.apply_item_value(tab)).pack(side="left", padx=(8, 12))
        ttk.Button(editor, text="一键设为上限 - 9", command=lambda: self.apply_item_safe_max(tab)).pack(side="left", padx=(0, 4))
        self.add_help(editor, "将勾选物品分别设为各自的实时上限减 9，留出余量防止游戏内继续获得时溢出。不同物品的上限独立计算。").pack(side="left")
        ttk.Label(page, textvariable=info["limit_var"], style="Hint.TLabel").grid(row=3, column=0, sticky="w", pady=(8, 0))

    def _param_entries(self, parent, variables):
        for index, name in enumerate(PARAM_NAMES):
            row, group = divmod(index, 3)
            ttk.Label(parent, text=name).grid(row=row, column=group * 2, padx=(0, 6), pady=3, sticky="w")
            ttk.Entry(parent, textvariable=variables[index], width=14).grid(row=row, column=group * 2 + 1, padx=(0, 22), pady=3, sticky="ew")

    def connect(self):
        try:
            self.game.connect()
            self.process_var.set(f"已连接：{GAME_NAME}（PID {self.game.pid}）")
            self.refresh()
        except Exception as exc:
            self.process_var.set("未连接")
            self.status_var.set("连接失败")
            messagebox.showerror("连接失败", str(exc), parent=self)

    def refresh(self):
        try:
            state = self.game.read_state()
            self.update_state(state)
            items = state["itemsByTab"]
            self.status_var.set(
                f"已读取资源、{len(state['crews'])} 名船员、武器 {len(items[0])} 件、道具 {len(items[1])} 件、"
                f"强化食物 {len(items[2])} 件与 {len(state['sugoroku']['cards'])} 张双六卡片"
            )
        except Exception as exc:
            self.status_var.set("读取失败")
            messagebox.showerror("读取失败", str(exc), parent=self)

    def update_state(self, state):
        self.state = state
        for key in self.resource_vars:
            self.resource_vars[key].set(str(state[key]))
        self.update_items(state["itemsByTab"])
        self.update_sugoroku(state["sugoroku"])
        items = state["itemsByTab"]
        self.summary_var.set(
            f"{len(state['crews'])} 名船员  ·  武器 {len(items[0])} 件  ·  道具 {len(items[1])} 件  ·  "
            f"强化食物 {len(items[2])} 件  ·  {len(state['sugoroku']['cards'])} 张双六卡片"
        )
        choices = [
            f"{crew['index'] + 1}. {self.display_name(crew['name'])}  Lv.{crew['level']}"
            for crew in state["crews"]
        ]
        self.crew_selector.configure(values=choices)
        if not choices:
            self.crew_choice_var.set("当前存档没有可编辑船员")
            return
        self.selected_crew = min(self.selected_crew, len(choices) - 1)
        self.crew_selector.current(self.selected_crew)
        self.load_crew_form()

    def update_items(self, items_by_tab):
        for tab, items in enumerate(items_by_tab):
            info = self.item_tabs[tab]
            tree = info["tree"]
            selected = set(tree.selection())
            valid_ids = {str(item["id"]) for item in items}
            info["checked"].intersection_update(valid_ids)
            tree.delete(*tree.get_children())
            for item in items:
                iid = str(item["id"])
                tree.insert(
                    "",
                    "end",
                    iid=iid,
                    values=(
                        "☑" if iid in info["checked"] else "☐",
                        self.display_name(item["name"], "未命名物品"),
                        item["count"],
                        item["maximum"],
                    ),
                )
            remaining = tuple(sorted(selected.intersection(valid_ids), key=int))
            tree.selection_set(remaining)
            self.on_items_selected(tab)

    def selected_item_indices(self, tab):
        info = self.item_tabs[tab]
        return sorted(int(iid) for iid in info["checked"] if info["tree"].exists(iid))

    def set_item_checked(self, tab, iid, checked):
        info = self.item_tabs[tab]
        if checked:
            info["checked"].add(iid)
        else:
            info["checked"].discard(iid)
        info["tree"].set(iid, "checked", "☑" if checked else "☐")
        self.on_items_selected(tab)

    def toggle_item_check(self, tab, event):
        tree = self.item_tabs[tab]["tree"]
        if tree.identify_region(event.x, event.y) != "cell" or tree.identify_column(event.x) != "#1":
            return None
        iid = tree.identify_row(event.y)
        if iid:
            self.set_item_checked(tab, iid, iid not in self.item_tabs[tab]["checked"])
        return "break"

    def update_item_check_marks(self, tab):
        info = self.item_tabs[tab]
        for iid in info["tree"].get_children():
            info["tree"].set(iid, "checked", "☑" if iid in info["checked"] else "☐")

    def select_all_items(self, tab):
        info = self.item_tabs[tab]
        info["checked"] = set(info["tree"].get_children())
        self.update_item_check_marks(tab)
        self.on_items_selected(tab)

    def clear_item_selection(self, tab):
        info = self.item_tabs[tab]
        info["checked"].clear()
        info["tree"].selection_set()
        self.update_item_check_marks(tab)
        self.on_items_selected(tab)

    def on_items_selected(self, tab):
        info = self.item_tabs[tab]
        indices = self.selected_item_indices(tab)
        if not indices:
            info["limit_var"].set("请点击“选择”列的复选框；可勾选多个物品")
            return
        items = {item["id"]: item for item in self.state["itemsByTab"][tab]}
        maximum = min(items[index]["maximum"] for index in indices)
        info["limit_var"].set(f"已勾选 {len(indices)} 项；本次可输入 0 到 {maximum}，应用时会再次实时检测")
        if len(indices) == 1:
            info["value_var"].set(str(items[indices[0]]["count"]))

    def apply_item_value(self, tab):
        indices = self.selected_item_indices(tab)
        if not indices:
            messagebox.showerror("无法应用", "请先勾选要修改的物品", parent=self)
            return
        try:
            items = {item["id"]: item for item in self.state["itemsByTab"][tab]}
            maximum = min(items[index]["maximum"] for index in indices)
            value = self.parse_number(self.item_tabs[tab]["value_var"].get(), "物品数量", maximum)
            result = self.game.apply_tab_items(tab, indices, value=value)
            self.update_state(result)
            self.status_var.set(f"已将 {len(indices)} 种物品数量写为 {value}，并回读校验成功")
        except Exception as exc:
            self.status_var.set("物品修改失败")
            messagebox.showerror("物品修改失败", str(exc), parent=self)

    def apply_item_safe_max(self, tab):
        indices = self.selected_item_indices(tab)
        if not indices:
            messagebox.showerror("无法应用", "请先勾选要修改的物品", parent=self)
            return
        try:
            result = self.game.apply_tab_items(tab, indices, safe_max=True)
            self.update_state(result)
            self.status_var.set(f"已将 {len(indices)} 种物品设为各自实时上限 - 9，并回读校验成功")
        except Exception as exc:
            self.status_var.set("物品修改失败")
            messagebox.showerror("物品修改失败", str(exc), parent=self)

    def update_sugoroku(self, sugoroku):
        locked = bool(sugoroku.get("stepLocked"))
        if self.lock_step_var.get() != locked:
            self.lock_step_var.set(locked)
        if locked:
            self.dice_count_var.set(str(sugoroku.get("stepLockDiceCount", 1)))
            self.dice_step_var.set(str(sugoroku.get("stepLockTotal", self.dice_step_var.get())))
        else:
            self.dice_step_var.set(str(sugoroku.get("step", 0)))
        self.dice_remaining_var.set(str(sugoroku["remaining"]))
        self.kairo_point_var.set(str(sugoroku.get("kairoPoint", 0)))
        if sugoroku.get("kairoReady", True):
            self.kairo_point_limit_var.set("可输入 0 到 2147483647，写入时通过游戏自身的数值对象安全应用")
        else:
            self.kairo_point_limit_var.set("开罗点数尚未初始化，请进入正常游戏界面后刷新")
        board = sugoroku.get("board") or {}
        if board.get("ready"):
            grid_max = board.get("gridMax", 0)
            self.board_info_var.set(
                f"当前棋盘：第 {board['level'] + 1} 关（本关共 {grid_max} 格）· 当前第 {board['pos']} 格"
            )
            if grid_max > 0:
                self.goto_spinbox.configure(to=grid_max)
        else:
            self.board_info_var.set("双六对局数据尚未初始化，进入双六屋一次后刷新即可读取棋盘状态")
            self.goto_spinbox.configure(to=200)
        selected = set(self.card_tree.selection())
        valid_ids = {str(card["index"]) for card in sugoroku["cards"]}
        self.checked_cards.intersection_update(valid_ids)
        self.card_tree.delete(*self.card_tree.get_children())
        for card in sugoroku["cards"]:
            iid = str(card["index"])
            self.card_tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    "☑" if iid in self.checked_cards else "☐",
                    self.display_name(card["name"], "未命名卡片"),
                    card["count"],
                    card["maximum"],
                ),
            )
        children = self.card_tree.get_children()
        remaining = tuple(sorted(selected.intersection(children), key=int))
        self.card_tree.selection_set(remaining)
        self.on_card_selected()

    def selected_card_indices(self):
        return sorted(int(iid) for iid in self.checked_cards if self.card_tree.exists(iid))

    def set_card_checked(self, iid, checked):
        if checked:
            self.checked_cards.add(iid)
        else:
            self.checked_cards.discard(iid)
        self.card_tree.set(iid, "checked", "☑" if checked else "☐")
        self.on_card_selected()

    def toggle_card_check(self, event):
        if self.card_tree.identify_region(event.x, event.y) != "cell" or self.card_tree.identify_column(event.x) != "#1":
            return None
        iid = self.card_tree.identify_row(event.y)
        if iid:
            self.set_card_checked(iid, iid not in self.checked_cards)
        return "break"

    def update_card_check_marks(self):
        for iid in self.card_tree.get_children():
            self.card_tree.set(iid, "checked", "☑" if iid in self.checked_cards else "☐")

    def select_all_cards(self):
        self.checked_cards = set(self.card_tree.get_children())
        self.update_card_check_marks()
        self.on_card_selected()

    def clear_card_selection(self):
        self.checked_cards.clear()
        self.card_tree.selection_set()
        self.update_card_check_marks()
        self.on_card_selected()

    def on_card_selected(self, _event=None):
        indices = self.selected_card_indices()
        if not indices:
            if not self.card_tree.get_children():
                self.card_limit_var.set("当前没有可修改的卡片；若尚未进入双六屋，请进入后点击“刷新数值”")
            else:
                self.card_limit_var.set("请点击“选择”列的复选框；可勾选多张卡片批量修改")
            return
        if not self.state:
            return
        cards = {card["index"]: card for card in self.state["sugoroku"]["cards"]}
        maximum = min(cards[index]["maximum"] for index in indices)
        self.card_limit_var.set(f"已勾选 {len(indices)} 张卡片；本次可输入 0 到 {maximum}，应用时会再次实时检测")
        if len(indices) == 1:
            self.card_value_var.set(str(cards[indices[0]]["count"]))

    def apply_card_value(self):
        indices = self.selected_card_indices()
        if not indices:
            messagebox.showerror("无法应用", "请先勾选要修改的卡片", parent=self)
            return
        try:
            cards = {card["index"]: card for card in self.state["sugoroku"]["cards"]}
            maximum = min(cards[index]["maximum"] for index in indices)
            value = self.parse_number(self.card_value_var.get(), "卡片数量", maximum)
            result = self.game.apply_cards(indices, value=value)
            self.update_state(result)
            self.status_var.set(f"已将 {len(indices)} 张卡片数量写为 {value}，并回读校验成功")
        except Exception as exc:
            self.status_var.set("卡片修改失败")
            messagebox.showerror("卡片修改失败", str(exc), parent=self)

    def apply_card_safe_max(self):
        indices = self.selected_card_indices()
        if not indices:
            messagebox.showerror("无法应用", "请先勾选要修改的卡片", parent=self)
            return
        try:
            result = self.game.apply_cards(indices, safe_max=True)
            self.update_state(result)
            self.status_var.set(f"已将 {len(indices)} 张卡片设为各自实时上限 - 1，并回读校验成功")
        except Exception as exc:
            self.status_var.set("卡片修改失败")
            messagebox.showerror("卡片修改失败", str(exc), parent=self)

    def apply_dice_step(self):
        try:
            count = int(self.dice_count_var.get())
            value = self.parse_number(self.dice_step_var.get(), "总步数", 99, count)
            result = self.game.apply_sugoroku({"step": value, "diceCount": count})
            if self.lock_step_var.get():
                self.game.set_step_lock(value, count)
                result = self.game.read_state()
                self.status_var.set(f"已锁定 {count} 颗骰子的总步数为 {value}，每次投掷都会重新应用")
            else:
                self.status_var.set(f"已将 {count} 颗骰子的总步数设为 {value}；下一次投掷将强制使用该步数")
            self.update_state(result)
        except Exception as exc:
            self.status_var.set("步数修改失败")
            messagebox.showerror("步数修改失败", str(exc), parent=self)

    def toggle_lock_step(self):
        if self.lock_step_var.get():
            try:
                count = int(self.dice_count_var.get())
                value = self.parse_number(self.dice_step_var.get(), "总步数", 99, count)
                result = self.game.apply_sugoroku({"step": value, "diceCount": count})
                self.game.set_step_lock(value, count)
                self.update_state(self.game.read_state())
                self.status_var.set(f"已启用步数锁定：{count} 颗骰子合计 {value} 步，每次投掷都会应用")
            except Exception as exc:
                self.lock_step_var.set(False)
                try:
                    self.game.clear_step_lock()
                except Exception:
                    pass
                self.status_var.set("步数锁定失败")
                messagebox.showerror("步数锁定失败", str(exc), parent=self)
        else:
            try:
                self.game.clear_step_lock()
            except Exception:
                pass
            self.status_var.set("已停用步数锁定")

    def apply_goto(self):
        try:
            if not self.state:
                raise ValueError("请先刷新读取数据")
            board = self.state["sugoroku"].get("board") or {}
            if not board.get("ready"):
                raise ValueError("双六对局数据尚未初始化，请进入双六屋后刷新")
            grid_max = board.get("gridMax", 0)
            target = self.parse_number(self.goto_var.get(), "目标格", grid_max, 1)
            current = board.get("pos", 0)
            if target <= current:
                raise ValueError(f"目标格必须大于当前所在格数（当前第 {current} 格）")
            result = self.game.set_goto_cell(target)
            self.goto_status_var.set(
                f"目标：第 {target} 格（第 {result['level'] + 1} 关，当前第 {result['pos']} 格，本关共 {result['gridMax']} 格）"
            )
            self.status_var.set(f"已设置目标格 {target}，下一次投掷将自动走过去")
        except Exception as exc:
            self.status_var.set("目标格设置失败")
            messagebox.showerror("目标格设置失败", str(exc), parent=self)

    def clear_goto(self):
        try:
            self.game.clear_goto_cell()
            self.goto_status_var.set("已取消目标格")
            self.status_var.set("已取消目标格")
        except Exception as exc:
            self.status_var.set("取消目标格失败")
            messagebox.showerror("取消目标格失败", str(exc), parent=self)

    def refresh_board(self):
        try:
            info = self.game.read_board()
            board = info.get("board") or {}
            if self.state is not None:
                self.state.setdefault("sugoroku", {})["board"] = board
            if board.get("ready"):
                grid_max = board.get("gridMax", 0)
                self.board_info_var.set(
                    f"当前棋盘：第 {board['level'] + 1} 关（本关共 {grid_max} 格）· 当前第 {board['pos']} 格"
                )
                if grid_max > 0:
                    self.goto_spinbox.configure(to=grid_max)
            else:
                self.board_info_var.set("双六对局数据尚未初始化，进入双六屋一次后刷新")
            notice = info.get("gotoNotice")
            if notice:
                self.goto_status_var.set(notice)
            elif info.get("gotoTarget") is not None:
                self.goto_status_var.set(f"目标：第 {info['gotoTarget']} 格（已设置，等待下一次投掷）")
            self.status_var.set("已刷新棋盘位置")
        except Exception as exc:
            self.status_var.set("刷新棋盘失败")
            messagebox.showerror("刷新棋盘失败", str(exc), parent=self)

    def apply_dice_remaining(self):
        try:
            info = self.game.read_sugoroku_remaining()
            maximum = info["maximum"]
            value = self.parse_number(self.dice_remaining_var.get(), "剩余投掷次数", maximum)
            if self.lock_remaining_var.get():
                self._lock_target_value = value
                result = self.game.write_sugoroku_remaining(value)
                self.dice_remaining_var.set(str(result["remaining"]))
                self._lock_failures = 0
                self.status_var.set(f"锁定目标已更新为 {value}")
                return
            result = self.game.apply_sugoroku({"remaining": value})
            self.update_state(result)
            self.status_var.set(f"已将剩余投掷次数设为 {value}，并回读校验成功")
        except Exception as exc:
            self.status_var.set("次数修改失败")
            messagebox.showerror("次数修改失败", str(exc), parent=self)

    def apply_kairo_point(self):
        try:
            value = self.parse_number(self.kairo_point_var.get(), "开罗点数", 2147483647)
            result = self.game.apply_sugoroku({"kairoPoint": value})
            self.update_state(result)
            self.status_var.set(f"已将开罗点数设为 {value}，并回读校验成功")
        except Exception as exc:
            self.status_var.set("开罗点数修改失败")
            messagebox.showerror("开罗点数修改失败", str(exc), parent=self)

    def toggle_lock_remaining(self):
        if self.lock_remaining_var.get():
            try:
                info = self.game.read_sugoroku_remaining()
                maximum = info["maximum"]
                target = self.parse_number(self.dice_remaining_var.get(), "剩余投掷次数", maximum)
                self.game.ensure_backup()
                self._lock_target_value = target
                self._lock_failures = 0
                if info["remaining"] != target:
                    result = self.game.write_sugoroku_remaining(target)
                    self.dice_remaining_var.set(str(result["remaining"]))
                self.status_var.set(f"已启用剩余投掷次数锁定（目标值：{target}，每 0.7 秒检查一次）")
                self._lock_remaining_loop()
            except Exception as exc:
                self.lock_remaining_var.set(False)
                self._stop_lock_remaining()
                self.status_var.set("锁定失败")
                messagebox.showerror("锁定失败", str(exc), parent=self)
        else:
            self._stop_lock_remaining()
            try:
                info = self.game.read_sugoroku_remaining()
                self.dice_remaining_var.set(str(info["remaining"]))
            except Exception:
                pass
            self.status_var.set("已停用剩余投掷次数锁定")

    def _lock_remaining_loop(self):
        if not self.lock_remaining_var.get():
            return
        try:
            info = self.game.read_sugoroku_remaining()
            target = min(self._lock_target_value, info["maximum"])
            self._lock_target_value = target
            if info["remaining"] != target:
                result = self.game.write_sugoroku_remaining(target)
                self.dice_remaining_var.set(str(result["remaining"]))
                self.status_var.set(f"已恢复剩余投掷次数为 {target}")
            self._lock_failures = 0
        except Exception as exc:
            self._lock_failures += 1
            self.status_var.set(f"锁定检查失败（{self._lock_failures}/3）：{exc}")
            if self._lock_failures >= 3:
                self.lock_remaining_var.set(False)
                self._stop_lock_remaining()
                self.status_var.set("锁定已停止：连续读取或写入失败，请进入游戏并刷新数值")
                return
        self._lock_timer_id = self.after(700, self._lock_remaining_loop)

    def _stop_lock_remaining(self):
        if self._lock_timer_id is not None:
            self.after_cancel(self._lock_timer_id)
            self._lock_timer_id = None
        self._lock_failures = 0

    def load_settings(self):
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def restore_column_widths(self):
        pairs = [(self.card_tree, "card_columns")]
        pairs += [(self.item_tabs[tab]["tree"], "item_columns") for tab in sorted(self.item_tabs)]
        for tree, key in pairs:
            saved = self.settings.get(key)
            if not isinstance(saved, dict):
                continue
            for name in ("checked", "name", "count", "maximum"):
                width = saved.get(name)
                if isinstance(width, int) and 20 <= width <= 2000:
                    tree.column(name, width=width)

    def schedule_save_column_widths(self, _event=None):
        if self._column_save_timer_id is not None:
            self.after_cancel(self._column_save_timer_id)
        self._column_save_timer_id = self.after(300, self.save_column_widths)

    def save_column_widths(self):
        self._column_save_timer_id = None
        changed = False
        pairs = [(self.card_tree, "card_columns")]
        pairs += [(self.item_tabs[tab]["tree"], "item_columns") for tab in sorted(self.item_tabs)]
        for tree, key in pairs:
            widths = {
                name: int(tree.column(name, "width"))
                for name in ("checked", "name", "count", "maximum")
            }
            if widths != self.settings.get(key):
                self.settings[key] = widths
                changed = True
        if not changed:
            return
        try:
            SETTINGS_FILE.write_text(
                json.dumps(self.settings, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def display_name(self, name, fallback="未命名船员"):
        cleaned = "".join(char for char in name if char.isprintable()).strip()
        return cleaned or fallback

    def on_crew_selected(self, _event):
        if self.state and self.state["crews"]:
            self.selected_crew = self.crew_selector.current()
            self.load_crew_form()

    def load_crew_form(self):
        crew = self.state["crews"][self.selected_crew]
        for key in self.crew_vars:
            self.crew_vars[key].set(str(crew[key]))
        for index in range(6):
            self.param_vars[index].set(str(crew["params"][index]))
            self.add_param_vars[index].set(str(crew["addParams"][index]))
        self.update_crew_headroom()

    def update_crew_headroom(self):
        try:
            totals = []
            for i in range(6):
                base_text = self.param_vars[i].get().strip()
                growth_text = self.add_param_vars[i].get().strip()
                base = int(base_text) if base_text else 0
                growth = int(growth_text) if growth_text else 0
                totals.append(base + growth)
        except ValueError:
            self.crew_headroom_var.set("⚠ 属性输入包含非数字内容，无法估算余量")
            self.crew_headroom_label.configure(foreground="#c62828")
            return
        limits = [PARAM_SAFE_TOTAL if PARAM_NAMES[i] in PARAM_SAFE_LIMITED else PARAM_SUM_LIMIT for i in range(6)]
        over = [PARAM_NAMES[i] for i, total in enumerate(totals) if total > limits[i]]
        parts = []
        for i, total in enumerate(totals):
            mark = "*" if PARAM_NAMES[i] in PARAM_SAFE_LIMITED else ""
            parts.append(f"{PARAM_NAMES[i]}{mark} 剩 {limits[i] - total:,}")
        text = "距安全上限余量：" + "，".join(parts)
        if over:
            text = "⚠ " + "、".join(over) + " 合计已超安全上限，应用会被拒绝。" + text
            self.crew_headroom_label.configure(foreground="#c62828")
        else:
            text += "（* 项探索/战斗含 32 位乘法，安全上限 999 万）"
            self.crew_headroom_label.configure(foreground="#6b7280")
        self.crew_headroom_var.set(text)

    def parse_number(self, value, label, maximum=2_000_000_000, minimum=0):
        try:
            number = int(value.strip(), 10)
        except ValueError as exc:
            raise ValueError(f"{label}必须是整数") from exc
        if not minimum <= number <= maximum:
            raise ValueError(f"{label}必须在 {minimum} 到 {maximum} 之间")
        return number

    def apply_resources(self):
        if not self.state:
            messagebox.showerror("无法应用", "请先刷新读取数据", parent=self)
            return
        try:
            changes = {key: self.parse_number(self.resource_vars[key].get(), label) for key, label in RESOURCE_FIELDS}
            changes["crews"] = []
            result = self.game.apply_changes(changes)
            self.update_state(result)
            self.status_var.set("基础资源已写入并回读校验成功；存档备份位于 backups 目录")
        except Exception as exc:
            self.status_var.set("资源应用失败")
            messagebox.showerror("资源应用失败", str(exc), parent=self)

    def check_crew_param_limits(self, crew):
        for i in range(6):
            total = crew["params"][i] + crew["addParams"][i]
            limit = PARAM_SAFE_TOTAL if PARAM_NAMES[i] in PARAM_SAFE_LIMITED else PARAM_SUM_LIMIT
            if total > limit:
                if limit == PARAM_SAFE_TOTAL:
                    raise ValueError(
                        f"{PARAM_NAMES[i]} 的基础 {crew['params'][i]} 与成长 {crew['addParams'][i]} 合计 {total}，"
                        f"超过 {limit}；该项在探索/战斗计算中含 32 位乘法（属性相性或技能百分比），"
                        "超出后会导致打不动怪、被秒杀或恢复异常，请调低后再应用"
                    )
                raise ValueError(
                    f"{PARAM_NAMES[i]} 的基础 {crew['params'][i]} 与成长 {crew['addParams'][i]} "
                    f"合计 {total}，超过 {limit}；游戏按 32 位整数求和，溢出后该属性会归为 1，请调低后再应用"
                )

    def apply_crew_restore(self):
        if not self.state or not self.state["crews"]:
            messagebox.showerror("无法应用", "没有可编辑的船员，请先刷新", parent=self)
            return
        try:
            changes = {key: self.state[key] for key in ("money", "food", "medals", "stamina")}
            crew = {"index": self.selected_crew, "restoreBattle": True}
            for key, var in self.crew_vars.items():
                crew[key] = self.parse_number(var.get(), key)
            crew["params"] = [self.parse_number(var.get(), f"基础{PARAM_NAMES[i]}") for i, var in enumerate(self.param_vars)]
            crew["addParams"] = [self.parse_number(var.get(), f"成长{PARAM_NAMES[i]}") for i, var in enumerate(self.add_param_vars)]
            self.check_crew_param_limits(crew)
            changes["crews"] = [crew]
            result = self.game.apply_changes(changes)
            self.update_state(result)
            restored = result["crews"][self.selected_crew]
            self.status_var.set(
                f"已将战斗 HP 恢复为 {restored['battleHP']}、战斗 SP 恢复为 {restored['battleSP']}（当前计算上限）"
            )
        except Exception as exc:
            self.status_var.set("恢复失败")
            messagebox.showerror("恢复失败", str(exc), parent=self)

    def apply_crew_max(self):
        if not self.state or not self.state["crews"]:
            messagebox.showerror("无法应用", "没有可编辑的船员，请先刷新", parent=self)
            return
        for var in self.param_vars:
            var.set(str(PARAM_ONECLICK_BASE))
        for var in self.add_param_vars:
            var.set(str(PARAM_ONECLICK_GROWTH))
        self.apply_crew()

    def apply_crew(self):
        if not self.state or not self.state["crews"]:
            messagebox.showerror("无法应用", "没有可编辑的船员，请先刷新", parent=self)
            return
        try:
            changes = {key: self.state[key] for key in ("money", "food", "medals", "stamina")}
            crew = {"index": self.selected_crew}
            for key, var in self.crew_vars.items():
                crew[key] = self.parse_number(var.get(), key)
            crew["params"] = [self.parse_number(var.get(), f"基础{PARAM_NAMES[i]}") for i, var in enumerate(self.param_vars)]
            crew["addParams"] = [self.parse_number(var.get(), f"成长{PARAM_NAMES[i]}") for i, var in enumerate(self.add_param_vars)]
            self.check_crew_param_limits(crew)
            changes["crews"] = [crew]
            result = self.game.apply_changes(changes)
            self.update_state(result)
            self.status_var.set("船员属性已写入并回读校验成功；存档备份位于 backups 目录")
        except Exception as exc:
            self.status_var.set("船员应用失败")
            messagebox.showerror("船员应用失败", str(exc), parent=self)

    def on_close(self):
        self._stop_lock_remaining()
        if self._column_save_timer_id is not None:
            self.after_cancel(self._column_save_timer_id)
            self._column_save_timer_id = None
        self.save_column_widths()
        self.game.close()
        self.destroy()


if __name__ == "__main__":
    TrainerApp().mainloop()