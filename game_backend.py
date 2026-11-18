import shutil
from datetime import datetime
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Lock

import frida

from app_settings import DEFAULT_BACKUP_ROOT, validate_backup_root

GAME_NAME = "KairoGames.exe"
GAME_SAVES = Path(r"E:\SteamLibrary\steamapps\common\High Sea Saga DX\saves")
BACKUP_ROOT = DEFAULT_BACKUP_ROOT
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
const fieldCache = new Map();
const offsetCache = new Map();
const methodCache = new Map();
const collectionCache = new Map();
function getMethod(klass, name, count) {
  const key = klass.toString() + ':' + name + ':' + count;
  if (!methodCache.has(key)) {
    const method = api.classMethod(klass, Memory.allocUtf8String(name), count);
    if (method.isNull()) throw new Error('找不到方法：' + name);
    methodCache.set(key, method);
  }
  return methodCache.get(key);
}
function getField(klass, name) {
  const key = klass.toString() + ':' + name;
  if (fieldCache.has(key)) return fieldCache.get(key);
  while (!klass.isNull()) {
    const iterator = Memory.alloc(POINTER);
    iterator.writePointer(NULL);
    for (;;) {
      const field = api.classFields(klass, iterator);
      if (field.isNull()) break;
      if (text(api.fieldName(field)) === name) {
        fieldCache.set(key, field);
        return field;
      }
    }
    klass = api.classParent(klass);
  }
  throw new Error('找不到字段：' + name);
}
function offset(klass, name) {
  const key = klass.toString() + ':' + name;
  if (!offsetCache.has(key)) offsetCache.set(key, api.fieldOffset(getField(klass, name)));
  return offsetCache.get(key);
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
let stepOverrideSource = null;
let stepOverrideApplied = false;
function clearStepOverride() {
  stepOverride = null;
  stepOverrideSource = null;
  stepOverrideApplied = false;
  stepOverridePos = -1;
  stepOverrideLevel = -1;
}
let stepOverridePos = -1;
let stepOverrideLevel = -1;
function stepPlan(total, count) {
  const base = Math.floor(total / count);
  const extra = total % count;
  const plan = [];
  for (let i = 0; i < count; i++) plan.push(base + (i < extra ? 1 : 0));
  return plan;
}
function failStepControl(message) {
  stepLockTotal = 0;
  stepLockDiceCount = 1;
  clearStepOverride();
  gotoTarget = null;
  gotoLevel = null;
  gotoNotice = message;
  send({type: 'error', message});
}
function applyStepValues(array, total) {
  if (array.isNull()) return false;
  const count = api.arrayLength(array);
  if (!Number.isInteger(total) || total < count || total > 99 || count < 1) {
    failStepControl('总步数 ' + total + ' 无法分配给本次实际 ' + count +
      ' 颗骰子（每颗至少 1 步，总步数不超过 99）；本次未写入骰子，步数锁定、单次覆盖和目标格均已停用，按游戏原骰子结果投掷');
    return false;
  }
  const plan = stepPlan(total, count);
  for (let i = 0; i < count; i++) {
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
  return true;
}
function applyStepLock(array) {
  if (stepLockTotal <= 0 || array.isNull()) return;
  applyStepValues(array, stepLockTotal);
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
  applyStepValues(array, walk);
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
          if (stepOverride !== null && (stepOverrideSource !== 'lock' || gotoTarget === null)) {
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
              clearStepOverride();
            } else {
              if (!applyStepValues(args[1], stepOverride)) return;
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
        } catch (e) {
          failStepControl('步数处理异常，可能已部分写入，请先刷新确认，不要直接重复应用。步数锁定、单次覆盖和目标格均已停用：' + e);
        }
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
  clearStepOverride();
  stepOverride = total;
  stepOverrideSource = 'lock';
  return {locked: true, total, diceCount: count};
}
function clearStepLock() {
  stepLockTotal = 0;
  stepLockDiceCount = 1;
  if (stepOverrideSource === 'lock') clearStepOverride();
  return {locked: false, stepOverridePending: stepOverride !== null, gotoTarget};
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
  const method = getMethod(klass, name, 0);
  if (method.isNull()) throw new Error('找不到方法：' + name);
  const exception = Memory.alloc(POINTER);
  exception.writePointer(NULL);
  const result = api.runtimeInvoke(method, object, NULL, exception);
  if (!exception.readPointer().isNull() || result.isNull()) throw new Error(name + ' 调用失败');
  return result.add(8).readS32();
}
function invokeInt1(klass, object, name, intArg) {
  const method = getMethod(klass, name, 1);
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
  const method = getMethod(klass, name, 1);
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
  const method = getMethod(klass, name, 0);
  if (method.isNull()) throw new Error('找不到方法：' + name);
  const exception = Memory.alloc(POINTER);
  exception.writePointer(NULL);
  api.runtimeInvoke(method, object, NULL, exception);
  if (!exception.readPointer().isNull()) throw new Error(name + ' 调用失败');
}
function setItemNum(item, value) {
  const method = getMethod(baseDataClass, 'SetNum', 1);
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
  const method = getMethod(exIntClass, 'Set', 1);
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
  const klass = api.objectClass(object);
  const key = klass.toString();
  if (!collectionCache.has(key)) {
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
    if (listField.isNull() && (itemsField.isNull() || sizeField.isNull())) {
      throw new Error('不支持的集合布局');
    }
    collectionCache.set(key, {
      list: listField.isNull() ? null : api.fieldOffset(listField),
      items: itemsField.isNull() ? null : api.fieldOffset(itemsField),
      size: sizeField.isNull() ? null : api.fieldOffset(sizeField)
    });
  }
  const layout = collectionCache.get(key);
  if (layout.list !== null) return collection(object.add(layout.list).readPointer());
  const items = object.add(layout.items).readPointer();
  const count = object.add(layout.size).readS32();
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
  if (wrapper.isNull()) throw new Error('数值封装对象尚未初始化');
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
function prepareSecureWrite(wrapper, value) {
  const target = secure(wrapper);
  target.object.add(target.originalOffset).readS64();
  target.object.add(target.valueOffset).readS64();
  target.object.add(target.bitmaskOffset).readS64();
  return {target, value};
}
function writeSecurePlan(plan) {
  const target = plan.target;
  const bitmask = target.object.add(target.bitmaskOffset).readS64();
  target.object.add(target.originalOffset).writeS64(new Int64(plan.value));
  target.object.add(target.valueOffset).writeS64(bitmask.xor(new Int64(plan.value)));
  if (target.object.add(target.originalOffset).readS64().toNumber() !== plan.value) {
    throw new Error('封装数值写入后校验失败');
  }
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
  const method = getMethod(coinManagerClass, 'GetInstance', 0);
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
function prepareMedalWrite(value) {
  const numbers = coinNumbers();
  const values = numbers.map(number => {
    if (number.isNull()) throw new Error('奖牌计数尚未初始化');
    const current = readCoinNumber(number);
    if (!Number.isSafeInteger(current) || current < 0) throw new Error('奖牌明细数值异常');
    return current;
  });
  const others = values.slice(1).reduce((total, current) => total + current, 0);
  if (!Number.isSafeInteger(others)) throw new Error('奖牌明细合计超出安全整数范围');
  const newFirstValue = value - others;
  if (newFirstValue < 0) throw new Error('目标奖牌数低于其它奖牌明细之和，无法安全应用');
  numbers[0].add(offset(secureLongClass, 'bitmask_')).readS64();
  numbers[0].add(offset(secureLongClass, 'value_')).readS64();
  return {numbers, value, newFirstValue};
}
function writeMedalPlan(plan) {
  const current = prepareMedalWrite(plan.value);
  if (current.numbers.length !== plan.numbers.length || current.numbers.some((number, index) =>
      number.toString() !== plan.numbers[index].toString()) || current.newFirstValue !== plan.newFirstValue) {
    throw new Error('奖牌明细在预检后已变化，请刷新确认');
  }
  writeCoinNumber(plan.numbers[0], plan.newFirstValue);
  if (readMedals() !== plan.value) throw new Error('奖牌总数写入后校验失败');
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
    step: stepOverride !== null ? stepOverride : (diceReady ? powers[0] : 0),
    powers,
    diceReady,
    stepLocked: stepLockTotal > 0,
    stepLockTotal,
    stepLockDiceCount,
    stepOverridePending: stepOverride !== null,
    gotoTarget,
    gotoNotice,
    board,
    remaining: invokeInt(exIntClass, playNum, 'Get'),
    maximum: staticInt(playerClass, 'SUGOROKU_PLAY_NUM'),
    kairoPoint: kairoWrapper.isNull() ? 0 : invokeInt(exIntClass, kairoWrapper, 'Get'),
    kairoReady: !kairoWrapper.isNull()
  };
}
function readResources() {
  const currentPlayer = player();
  return {
    money: readSecure(currentPlayer.add(moneyFieldOffset).readPointer()),
    food: readSecure(researchWrapper(currentPlayer, 0)),
    medals: readMedals(),
    stamina: readSecure(staminaWrapper(currentPlayer, 2))
  };
}
function readCrews() {
  return crewObjects(player()).map(readCrew);
}
function readItems(tab) {
  if (!Number.isInteger(tab) || tab < 0 || tab > 2) throw new Error('物品分页必须在 0 到 2 之间');
  return readItemsByTab(tab);
}
function readState() {
  return Object.assign(readResources(), {
    crews: readCrews(),
    itemsByTab: [readItems(0), readItems(1), readItems(2)],
    sugoroku: readSugoroku(player())
  });
}
function writeIntArray(object, fieldOffset, values) {
  const array = object.add(fieldOffset).readPointer();
  if (array.isNull() || api.arrayLength(array) < 6) throw new Error('属性数组异常');
  for (let i = 0; i < 6; i++) {
    array.add(16 + i * 4).writeS32(values[i]);
    if (array.add(16 + i * 4).readS32() !== values[i]) throw new Error('属性写入后校验失败');
  }
}
function checkInteger(value, label, maximum = 2000000000) {
  if (!Number.isInteger(value) || value < 0 || value > maximum) {
    throw new Error(label + '必须在 0 到 ' + maximum + ' 之间');
  }
}
function applyChanges(changes) {
  const currentPlayer = player();
  const crewChanges = changes.crews === undefined ? [] : changes.crews;
  if (!Array.isArray(crewChanges)) throw new Error('船员修改必须为列表');
  const crews = crewChanges.length ? crewObjects(currentPlayer) : [];
  const resourceKeys = ['money', 'food', 'medals', 'stamina'].filter(key =>
    Object.prototype.hasOwnProperty.call(changes, key));
  for (const key of resourceKeys) checkInteger(changes[key], key);
  const PARAM_NAME_TABLE = ['HP', 'SP', '攻击', '防御', '速度', '智慧'];
  const PARAM_SAFE_TOTAL = 9990000;
  const PARAM_SAFE_LIMITED = ['HP', 'SP', '攻击', '防御', '速度'];
  for (const change of crewChanges) {
    if (!Number.isInteger(change.index) || change.index < 0 || change.index >= crews.length) {
      throw new Error('船员列表已变化，请刷新后重试');
    }
    if (change.restoreBattle === true) continue;
    for (const key of ['level', 'exp', 'battleHP', 'battleSP']) checkInteger(change[key], key);
    if (!Array.isArray(change.params) || change.params.length !== 6 ||
        !Array.isArray(change.addParams) || change.addParams.length !== 6) {
      throw new Error('船员基础与成长属性必须各有六项');
    }
    for (let i = 0; i < 6; i++) {
      checkInteger(change.params[i], PARAM_NAME_TABLE[i]);
      checkInteger(change.addParams[i], PARAM_NAME_TABLE[i]);
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
  const resourcePlans = {};
  if (resourceKeys.includes('money')) {
    resourcePlans.money = prepareSecureWrite(currentPlayer.add(moneyFieldOffset).readPointer(), changes.money);
  }
  if (resourceKeys.includes('food')) resourcePlans.food = prepareSecureWrite(researchWrapper(currentPlayer, 0), changes.food);
  if (resourceKeys.includes('medals')) resourcePlans.medals = prepareMedalWrite(changes.medals);
  if (resourceKeys.includes('stamina')) resourcePlans.stamina = prepareSecureWrite(staminaWrapper(currentPlayer, 2), changes.stamina);
  const restoredBattle = new Map();
  for (const change of crewChanges) {
    const crew = crews[change.index];
    crew.add(battleHPOffset).readS32();
    crew.add(battleSPOffset).readS32();
    if (change.restoreBattle === true) {
      restoredBattle.set(change, [invokeInt1(characterClass, crew, 'GetParam', 0), invokeInt1(characterClass, crew, 'GetParam', 1)]);
    } else {
      crew.add(levelOffset).readS32();
      crew.add(expOffset).readS32();
      intArray(crew, paramsOffset);
      intArray(crew, addParamsOffset);
    }
  }
  if (resourcePlans.medals) writeMedalPlan(resourcePlans.medals);
  for (const key of ['money', 'food', 'stamina']) {
    if (resourcePlans[key]) writeSecurePlan(resourcePlans[key]);
  }
  const writtenCrews = [];
  for (const change of crewChanges) {
    const crew = crews[change.index];
    let battleHP;
    let battleSP;
    if (change.restoreBattle === true) {
      [battleHP, battleSP] = restoredBattle.get(change);
    } else {
      writeInt(crew, levelOffset, change.level);
      writeInt(crew, expOffset, change.exp);
      writeIntArray(crew, paramsOffset, change.params);
      writeIntArray(crew, addParamsOffset, change.addParams);
      battleHP = change.battleHP;
      battleSP = change.battleSP;
    }
    writeInt(crew, battleHPOffset, battleHP);
    writeInt(crew, battleSPOffset, battleSP);
    writtenCrews.push({index: change.index, battleHP, battleSP});
  }
  return {applied: true, resources: resourceKeys, crews: writtenCrews};
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
  return {applied: true, tab: changes.tab, count: plans.length};
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
    if (!stepLockReady) throw new Error('步数挂钩安装失败，请重新连接游戏');
    clearStepOverride();
    stepOverride = changes.step;
    stepOverrideSource = stepLockTotal > 0 ? 'lock' : 'once';
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
  return {applied: true};
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
  readResources,
  readCrews,
  readItems,
  readSugoroku() { return readSugoroku(player()); },
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


class IL2CPPGame:
    def __init__(self, backup_root=None):
        self.session = None
        self.script = None
        self.pid = None
        self.backup_created = False
        self.backup_root = validate_backup_root(DEFAULT_BACKUP_ROOT if backup_root is None else backup_root)
        self._events = SimpleQueue()
        self._event_lock = Lock()
        self._session_token = 0
        self._cleanup_pending = False

    def _on_message(self, token, message, _data):
        kind = message.get("type", "message")
        if kind == "error":
            detail = message.get("stack") or message.get("description") or str(message)
            event = {"type": "error", "message": str(detail)}
        else:
            detail = message.get("payload", message)
            if isinstance(detail, dict) and detail.get("type") in ("error", "message"):
                event = {"type": detail["type"], "message": str(detail.get("message", detail))}
            else:
                event = {"type": "message", "message": str(detail)}
        self._events.put((token, event))

    def _on_detached(self, token, reason, crash=None):
        detail = str(reason)
        if crash is not None:
            detail += f"：{crash}"
        self._events.put((token, {"type": "detached", "message": detail}))

    def drain_events(self):
        result = []
        while True:
            try:
                token, event = self._events.get_nowait()
            except Empty:
                break
            with self._event_lock:
                if token != self._session_token:
                    continue
                if event["type"] == "detached":
                    self._session_token += 1
                    self.script = None
                    self.session = None
                    self.pid = None
                    self.backup_created = False
                    self._cleanup_pending = False
                result.append(event)
        return result

    def connect(self):
        self.close()
        device = frida.get_local_device()
        process = next((p for p in device.enumerate_processes() if p.name.lower() == GAME_NAME.lower()), None)
        if process is None:
            raise RuntimeError("未找到 KairoGames.exe，请先启动游戏并载入存档")
        token = self._session_token
        session = device.attach(process.pid)
        with self._event_lock:
            self.session = session
            self.pid = process.pid
        try:
            session.on("detached", lambda reason, crash=None: self._on_detached(token, reason, crash))
            script = session.create_script(IL2CPP_SCRIPT)
            with self._event_lock:
                self.script = script
            script.on("message", lambda message, data: self._on_message(token, message, data))
            script.load()
            with self._event_lock:
                if token != self._session_token or session.is_detached:
                    raise RuntimeError("游戏连接已断开，请重新连接")
        except Exception as exc:
            try:
                self.close()
            except Exception as cleanup_error:
                raise RuntimeError(f"连接失败：{exc}；清理失败：{cleanup_error}") from exc
            raise

    def _exports(self):
        with self._event_lock:
            if self._cleanup_pending:
                raise RuntimeError("上次关闭未能确认释放挂钩，请先重试断开连接，当前禁止读写或建立新会话")
            if self.script is None or (self.session is not None and self.session.is_detached):
                raise RuntimeError("请先连接游戏")
            return self.script.exports_sync

    def _write(self, method, *args):
        self._exports()
        try:
            self.ensure_backup()
        except Exception as exc:
            raise RuntimeError(f"备份保护未建立，本次未调用写入 RPC：{exc}") from exc
        exports = self._exports()
        try:
            return getattr(exports, method)(*args)
        except Exception as exc:
            raise RuntimeError(f"可能已部分写入，请先刷新确认，不要直接重复应用。原始错误：{exc}") from exc

    def read_state(self):
        return self._exports().read_state()

    def read_resources(self):
        return self._exports().read_resources()

    def read_crews(self):
        return self._exports().read_crews()

    def read_items(self, tab):
        return self._exports().read_items(tab)

    def read_sugoroku(self):
        return self._exports().read_sugoroku()

    def apply_changes(self, changes):
        return self._write("apply_changes", changes)

    def apply_tab_items(self, tab, ids, value=None, safe_max=False):
        return self._write("apply_item_tab_changes", {
            "tab": tab,
            "ids": ids,
            "value": value,
            "safeMax": safe_max,
        })

    def apply_sugoroku(self, changes):
        return self._write("apply_sugoroku_changes", changes)

    def apply_cards(self, indices, value=None, safe_max=False):
        return self._write("apply_sugoroku_changes", {
            "cards": indices,
            "cardValue": value,
            "safeMax": safe_max,
        })

    def read_sugoroku_remaining(self):
        return self._exports().read_sugoroku_remaining()

    def write_sugoroku_remaining(self, value):
        return self._write("write_sugoroku_remaining", value)

    def set_step_lock(self, total, count):
        return self._write("set_step_lock", total, count)

    def clear_step_lock(self):
        return self._exports().clear_step_lock()

    def set_goto_cell(self, target):
        return self._write("set_goto_cell", target)

    def clear_goto_cell(self):
        return self._exports().clear_goto_cell()

    def read_board(self):
        return self._exports().read_board_state()

    def set_backup_root(self, path):
        root = validate_backup_root(path)
        if root == self.backup_root:
            return
        with self._event_lock:
            token = self._session_token
            protected = self.backup_created and self.script is not None
        if protected:
            self._copy_backup(root)
        with self._event_lock:
            self.backup_root = root
            self.backup_created = protected and token == self._session_token and self.script is not None

    def ensure_backup(self):
        self._exports()
        with self._event_lock:
            if self.backup_created:
                return
            token = self._session_token
            root = self.backup_root
        self.backup_saves()
        with self._event_lock:
            if token != self._session_token or root != self.backup_root:
                raise RuntimeError("备份期间连接或备份目录已变化，请重试")
            self.backup_created = True

    def _copy_backup(self, root):
        root = validate_backup_root(root)
        if not GAME_SAVES.is_dir():
            raise RuntimeError(f"找不到存档目录：{GAME_SAVES}")
        destination = root / datetime.now().strftime("%Y%m%d-%H%M%S-%f") / "saves"
        destination.parent.mkdir(parents=True, exist_ok=False)
        shutil.copytree(GAME_SAVES, destination)
        return destination

    def backup_saves(self):
        return self._copy_backup(self.backup_root)

    @staticmethod
    def _release(script, session):
        script_released = False
        session_released = False
        errors = []
        if script is not None:
            try:
                script.unload()
                script_released = True
            except Exception as exc:
                errors.append(f"script.unload：{exc}")
        if session is not None:
            try:
                session.detach()
                session_released = True
            except Exception as exc:
                errors.append(f"session.detach：{exc}")
                try:
                    session_released = bool(session.is_detached)
                except Exception as state_error:
                    errors.append(f"读取断开状态：{state_error}")
        if script is None and session is None:
            return
        if not script_released and not session_released:
            detail = "；".join(errors)
            raise RuntimeError(f"无法确认挂钩已释放，已保留会话供重试，禁止重新连接：{detail}")

    def close(self):
        with self._event_lock:
            script, session = self.script, self.session
            self._cleanup_pending = True
            self.backup_created = False
        self._release(script, session)
        with self._event_lock:
            self._session_token += 1
            self.script = None
            self.session = None
            self.pid = None
            self._cleanup_pending = False

