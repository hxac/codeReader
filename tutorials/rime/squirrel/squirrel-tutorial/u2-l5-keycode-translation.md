# macOS 到 Rime 的按键映射

## 1. 本讲目标

在上一讲（u2-l4）里，我们已经看清了 `handle(_:client:)` 这条键盘事件主循环：系统把 `NSEvent` 送进来，Squirrel 按 `.keyDown` / `.flagsChanged` 分发，最后统一调用 `rimeUpdate()` 刷新面板。但我们在上一讲里刻意跳过了一个关键细节——

> macOS 的按键，到底是怎么变成 librime 能看懂的按键的？

本讲就来补上这块拼图。读完本讲，你应当能够：

1. 说清楚一个 `NSEvent` 的「修饰键掩码」是如何被翻译成 Rime 的修饰键掩码的。
2. 说清楚一个按键有「字符」和「虚拟键码」两条翻译路径，以及为什么 Squirrel 要同时维护两条。
3. 理解远程桌面软件发送 `keyCode = 0` 时，Squirrel 如何靠 `inferModifierKeycode` 兜底推断。
4. 理解为什么字母键被故意放进「次优先」的 `additionalCodeMappings`，而不是和功能键一起放进「最优先」的 `keycodeMappings`。

本讲只深读一个文件：`sources/MacOSKeyCodes.swift`，并在 `sources/SquirrelInputController.swift` 中查看它的调用点。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 macOS 和 Rime 用的是两套按键世界

macOS 用 **Apple 虚拟键码（virtual keycode）** 来标识物理按键，比如「A 键」是 `kVK_ANSI_A = 0`，「左 Shift」是 `kVK_Shift = 56`。这些码和键盘布局、大小写无关，只认物理位置。它们来自 `import Carbon`（见 `MacOSKeyCodes.swift` 顶部）。

而 librime 走的是 **X11 keysym 体系**（来自 Linux/IBus 传统），用 `XK_` 前缀的常量标识「键的含义」：`XK_a = 0x61`、`XK_A = 0x41`、`XK_Shift_L = 0xFFE1`、`XK_Return = 0xFF0D`。注意 `XK_a` 和 `XK_A` 是**两个不同的码**——X11 区分大小写字母键。

所以 Squirrel 作为 macOS 前端，必须做一层翻译：把 Apple 的「物理键码 + 当前修饰键状态 + 产生的字符」组合，翻译成 librime 期望的「一个 keysym + 一个修饰键掩码」。这层翻译就是 `SquirrelKeycode`。

### 2.2 修饰键是「位掩码」

修饰键（Shift / Control / Option / Command / CapsLock）不是单独的按键事件，而是附加在每个按键上的**状态标记**。macOS 用 `NSEvent.ModifierFlags`（一个 `OptionSet`，每个修饰键占一个比特）来表达「这一刻有哪些修饰键被按住」。

Rime 也用位掩码表达修饰键，但位的定义和命名来自 X11 传统，常量名带 `k` 前缀：`kShiftMask`、`kLockMask`（CapsLock）、`kControlMask`、`kAltMask`（Option）、`kSuperMask`（Command），外加一个特殊的 `kReleaseMask`（标记「松开」）。这些常量都来自 librime 头文件 `<rime/key_table.h>`（经 `Squirrel-Bridging-Header.h` 引入 Swift）。

> 关键认知：**修饰键在两边都是「一个比特」，但同一个修饰键在两边占的比特位置不同**，所以不能直接传，必须逐位重新拼装。

### 2.3 同一个按键，有两种翻译依据

一个按键事件里同时携带两份信息：

- **虚拟键码 `keyCode`**：物理位置，固定不变。
- **产生的字符 `characters` / `charactersIgnoringModifiers`**：受键盘布局、Shift、CapsLock 影响后实际打出的字符。

Squirrel 的策略是：**能用字符就用字符**（因为字符自带大小写、自带 Shift 后的符号，最贴近 X11 keysym 的语义）；字符拿不到时，再退回到「按键码表」按物理位置查。这就是本讲后面会反复出现的两条路径。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `sources/MacOSKeyCodes.swift` | 全部按键翻译逻辑都在这里，是一个无状态的 `struct SquirrelKeycode`，只暴露静态方法与两张码表。 |
| `sources/SquirrelInputController.swift` | 调用方。`handle` 里的 `.keyDown` 与 `.flagsChanged` 两个分支分别调用翻译函数，再把结果交给 `processKey` → `rimeAPI.process_key`。 |
| `sources/Squirrel-Bridging-Header.h` | 引入 `<rime/key_table.h>`，提供 `XK_*` keysym 常量与 `k*Mask` 修饰键掩码常量。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，正好对应 `SquirrelKeycode` 的四个对外接口。

### 4.1 osxModifiersToRime：修饰键掩码翻译

#### 4.1.1 概念说明

这是四个函数里最直白的一个。它的职责是：把 `NSEvent.ModifierFlags`（macOS 的修饰键集合）逐位翻译成 Rime 的修饰键掩码（一个 `UInt32`）。

为什么不能直接传？因为两边的「比特位定义」不同。例如 macOS 的 `.shift` 和 Rime 的 `kShiftMask` 各自占自己的比特位，数值上不相等。所以必须按语义逐项「搬运」：见到 `.shift` 就把 Rime 掩码里的 `kShiftMask` 那一位置 1，依此类推。

注意一个语义映射的细节：macOS 的 `.option` 对应 Rime 的 `kAltMask`（Alt），macOS 的 `.command` 对应 Rime 的 `kSuperMask`（Super/Win 键）。这是跨平台输入法常见的命名错位。

#### 4.1.2 核心流程

可以把它理解为一个「按位或」累加器：

```
ret = 0
若含 .capsLock  → ret |= kLockMask
若含 .shift     → ret |= kShiftMask
若含 .control   → ret |= kControlMask
若含 .option    → ret |= kAltMask
若含 .command   → ret |= kSuperMask
返回 ret
```

用集合论的语言，设 macOS 修饰键集合为 \( M \)，Rime 掩码为 \( R \)，则：

\[
R = \bigoplus_{m \in M} \text{mask}(m)
\]

其中 \( \bigoplus \) 表示按位或，\( \text{mask}(\cdot) \) 是 macOS 修饰键到 Rime 掩码位的一对一映射。这里之所以用「按位或」而不是「相加」，是因为每个掩码只占独立的一个比特，按位或和相加在「每位最多置一次」时结果相同，但按位或更安全（重复置位不会进位）。

#### 4.1.3 源码精读

[MacOSKeyCodes.swift:13-31](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/MacOSKeyCodes.swift#L13-L31) 给出了完整实现。注意五次 `if ... ret |= ...`，正是上面流程里五项一对一搬运：

```swift
static func osxModifiersToRime(modifiers: NSEvent.ModifierFlags) -> UInt32 {
  var ret: UInt32 = 0
  if modifiers.contains(.capsLock) { ret |= kLockMask.rawValue }
  if modifiers.contains(.shift)    { ret |= kShiftMask.rawValue }
  if modifiers.contains(.control)  { ret |= kControlMask.rawValue }
  if modifiers.contains(.option)   { ret |= kAltMask.rawValue }
  if modifiers.contains(.command)  { ret |= kSuperMask.rawValue }
  return ret
}
```

几点值得注意：

- 函数只翻译这五种修饰键。macOS 还有的 `.function`、`.numericPad`、`.help` 等**不会被翻译**——它们在 Rime 的按键模型里没有对应概念，直接丢弃。
- `.capsLock` 被映射成 `kLockMask`（Lock，即大写锁定），而不是某个 Shift 位。这一点很重要，它和上一讲 `flagsChanged` 分支里 capslock 的「异或回拨」直接相关。
- 调用方在 [SquirrelInputController.swift:113](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L113)（keyDown 分支）与 [:59](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L59)（flagsChanged 分支）都用了它。

#### 4.1.4 代码实践

**实践目标**：亲手验证「修饰键逐位搬运」的结果，建立对掩码数值的直觉。

**操作步骤**（源码阅读型实践，无需运行 Squirrel）：

1. 打开 librime 的 `<rime/key_table.h>`（在你本地 `librime/` 子模块的 `include/rime/` 或 `src/rime/` 下；本仓库未签出该子模块时可去 [librime 仓库](https://github.com/rime/librime) 查阅），找到 `kShiftMask`、`kLockMask`、`kControlMask`、`kAltMask`、`kSuperMask` 的定义值。
2. 假设用户按下 `Shift + Control`（无其它修饰键），手算 `osxModifiersToRime` 的返回值：`kShiftMask | kControlMask`。
3. 再假设按下 `CapsLock + Command`，手算返回值。

**需要观察的现象**：每个修饰键在结果里贡献且仅贡献一个独立的比特位；任意两个修饰键的掩码按位或，都不会互相覆盖。

**预期结果**：返回值是若干「单比特常量」的按位或；具体数值取决于 librime 的定义，待本地确认子模块后核对。如果你把结果用二进制打印，应看到恰好两个比特为 1。

> 待本地验证：精确的十进制/十六进制值需对照本地 librime `key_table.h`。

#### 4.1.5 小练习与答案

**练习 1**：为什么这个函数用「按位或 `|=`」而不是「相加 `+=`」来累加？

**参考答案**：因为每个掩码常量只占一个独立的比特位，且同一修饰键在一次事件里至多出现一次。用按位或语义上更准确（表达「置位」而非「计数」），也能避免万一某位重复时相加导致的进位错误。

**练习 2**：macOS 的 `.command` 被翻译成了哪个 Rime 掩码？为什么名字对不上？

**参考答案**：翻译成 `kSuperMask`。因为 Rime 沿用 X11/IBus 传统，把「Windows/Super 键位」叫 Super；而 macOS 的主修饰键是 Command，物理语义上对应 Linux 的 Super 键，于是做了这层命名映射。同理 `.option` → `kAltMask`。

---

### 4.2 osxKeycodeToRime：字符 / 码表两条映射路径

#### 4.2.1 概念说明

这是整个翻译层最核心、也最精巧的函数。它把「一个物理按键」翻译成「一个 X11 keysym」。难点在于：同一个物理按键，在不同 Shift / CapsLock 状态下含义不同（`a` 还是 `A`？`1` 还是 `!`？），而 librime 期望收到的是**已经反映大小写状态的 keysym**。

Squirrel 的解法是「**三段式优先级**」：

1. **先查主码表 `keycodeMappings`**：如果这个物理键码在主码表里（功能键、方向键、小键盘、修饰键等），直接返回——这些键与字符无关，必须按物理位置翻译。
2. **再走字符路径**：如果事件里带了可用字符，按字符（必要时翻转大小写）算出 keysym。字母、数字、标点走这一路。
3. **最后查备用码表 `additionalCodeMappings`**：字符路径走不通时（典型场景：`flagsChanged` 里字符为 `nil`），按物理键码兜底。
4. 全都查不到，返回 `XK_VoidSymbol`（表示「无效」）。

#### 4.2.2 核心流程

伪代码如下（对应源码的层层 `if let`）：

```
函数 osxKeycodeToRime(keycode, keychar, shift, caps):

  # 第 1 段：主码表（功能/方向/小键盘/修饰...）
  if keycode in keycodeMappings:
      return keycodeMappings[keycode]

  # 第 2 段：字符路径
  if keychar 是 ASCII 字符:
      codeValue = keychar 的 unicode 值
      # 字母的大小写翻转
      if keychar 是小写字母 and (shift 与 caps 恰好其一为真):
          return keychar 转大写后的值        # a -> A (XK_A)
      # 可打印 ASCII 直接用其码值（X11 keysym == ASCII）
      switch codeValue:
        0x20..0x7e -> return codeValue        # 空格..~ 的可见 ASCII
        0x1b       -> return XK_bracketleft   # 控制字符回退到方括号键
        0x1c       -> return XK_backslash
        0x1d       -> return XK_bracketright
        0x1f       -> return XK_minus

  # 第 3 段：备用码表（字母/数字/标点，按物理键码）
  if keycode in additionalCodeMappings:
      return additionalCodeMappings[keycode]

  # 第 4 段：无效
  return XK_VoidSymbol
```

这里最巧妙的是第 2 段里的**大小写翻转条件** `shift != caps`。它是一个异或（XOR）：

\[
\text{翻转} = \text{shift} \oplus \text{caps}
\]

含义是「Shift 和 CapsLock 恰好有一个生效时，字母才翻转大小写」。因为 Shift 和 CapsLock 对字母大小写的作用都是「取反」，两者同时生效时会**互相抵消**（Shift + CapsLock + a 仍是小写 a）。这正好符合真实键盘行为。

至于 `0x1b / 0x1c / 0x1d / 0x1f` 这几个控制字符被映射到方括号/反斜杠/减号，是继承了 X11 里对 VT100 终端按键码的历史约定，属于兼容性处理，平时几乎不会触发。

#### 4.2.3 源码精读

完整实现见 [MacOSKeyCodes.swift:33-66](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/MacOSKeyCodes.swift#L33-L66)。关键片段：

```swift
static func osxKeycodeToRime(keycode: UInt16, keychar: Character?, shift: Bool, caps: Bool) -> UInt32 {
  // 第 1 段：主码表
  if let code = keycodeMappings[Int(keycode)] { return UInt32(code) }

  // 第 2 段：字符路径
  if let keychar = keychar, keychar.isASCII, let codeValue = keychar.unicodeScalars.first?.value {
    // IBus/Rime 用不同的码区分大小写字母
    if keychar.isLowercase && (shift != caps) {
      return keychar.uppercased().unicodeScalars.first!.value   // a -> A
    }
    switch codeValue {
    case 0x20...0x7e: return codeValue                          // 可见 ASCII，keysym == ASCII
    case 0x1b:        return UInt32(XK_bracketleft)
    case 0x1c:        return UInt32(XK_backslash)
    case 0x1d:        return UInt32(XK_bracketright)
    case 0x1f:        return UInt32(XK_minus)
    default: break
    }
  }

  // 第 3 段：备用码表
  if let code = additionalCodeMappings[Int(keycode)] { return UInt32(code) }

  // 第 4 段：无效
  return UInt32(XK_VoidSymbol)
}
```

调用点最能说明它的用法。`keyDown` 分支带着字符调用（[SquirrelInputController.swift:108-114](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L108-L114)）：

```swift
let rimeKeycode = SquirrelKeycode.osxKeycodeToRime(
  keycode: keyCode, keychar: char,
  shift: modifiers.contains(.shift), caps: modifiers.contains(.capsLock))
if rimeKeycode != 0 {
  let rimeModifiers = SquirrelKeycode.osxModifiersToRime(modifiers: modifiers)
  handled = processKey(rimeKeycode, modifiers: rimeModifiers)
  ...
}
```

注意 `if rimeKeycode != 0` 这层守卫：`XK_VoidSymbol` 是个很大的非零值，这里挡的主要是「翻译不出有效字符」的极端情况（`char` 取不到时根本进不来）。而 `flagsChanged` 分支则**不带字符**调用（[:71](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L71)）：

```swift
let rimeKeycode = SquirrelKeycode.osxKeycodeToRime(
  keycode: keyCode, keychar: nil, shift: false, caps: false)
```

`flagsChanged` 事件本身没有「字符」概念，所以 `keychar` 传 `nil`，强制走「码表」路径——修饰键的物理键码都在 `keycodeMappings` 主码表里，第 1 段就能命中。

#### 4.2.4 代码实践

**实践目标**：追踪一次「Shift + a」按键，亲手算出最终传给 `rimeAPI.process_key` 的 keycode 与 modifier。这是本讲的主实践，也是规格里指定的实践任务。

**操作步骤**（源码追踪型实践）：

1. 假设用户按住 Shift 再按 A 键，产生一个 `.keyDown` 事件。
2. 在 [SquirrelInputController.swift:95-117](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L95-L117) 的 `keyDown` 分支里，先确认 `modifiers.contains(.command)` 为 false（没有 Command），不会被放行。
3. 读出事件三要素：
   - `keyCode = kVK_ANSI_A = 0`
   - `modifiers = { .shift }`（只按了 Shift）
   - `charactersIgnoringModifiers = "a"`（忽略 Shift 后的基础字符）
4. 确认 `keyChars` 的取值。`capitalModifiers = modifiers.isSubset(of: [.shift, .capsLock])` 为 true（`{.shift}` 是 `{.shift, .capsLock}` 的子集）；但 `code = "a"` 是字母，`(capitalModifiers && !code.isLetter)` 为 false，故 `keyChars` 保持 `"a"`（见 [:103-107](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L103-L107)）。
5. 于是以 `osxKeycodeToRime(keycode: 0, keychar: "a", shift: true, caps: false)` 调用翻译函数，逐段判断：
   - 第 1 段：`keycodeMappings[0]`——主码表里**没有**字母键，返回 nil，跳过。
   - 第 2 段：`"a"` 是 ASCII 小写字母，`shift != caps` 即 `true != false` 为 true，触发翻转 → 返回 `"A"` 的 unicode 值。
6. 翻译结果 `rimeKeycode = 0x41`（即 `XK_A = 65`）。
7. 修饰键：`osxModifiersToRime(modifiers: { .shift })` → 只有 `.shift` 命中 → 返回 `kShiftMask`。
8. 最终调用 `processKey(0x41, modifiers: kShiftMask)`，在 [SquirrelInputController.swift:401](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L401) 落到 `rimeAPI.process_key(session, 0x41, kShiftMask)`。

**需要观察的现象**：翻译送给引擎的 keysym 是**大写** `XK_A`（65），而不是小写 `XK_a`（97）；修饰键掩码里**还同时带着** `kShiftMask`。也就是说「Shift 状态被表达了两遍」——一遍体现在 keysym 的大小写，一遍体现在修饰键掩码。这恰好符合 X11/IBus 的按键语义。

**预期结果**：

| 量 | 值 |
| --- | --- |
| `keyCode`（macOS 物理键码） | `0`（`kVK_ANSI_A`） |
| `keychar` | `"a"` |
| `rimeKeycode`（keysym） | `0x41` / 65（`XK_A`） |
| `rimeModifiers` | `kShiftMask` |
| 调用 | `rimeAPI.process_key(session, 65, kShiftMask)` |

#### 4.2.5 小练习与答案

**练习 1**：如果用户**同时**按住 Shift 和开启 CapsLock，再按 a 键，`osxKeycodeToRime` 会返回 `XK_a` 还是 `XK_A`？

**参考答案**：返回 `XK_a`（小写，0x61）。因为此时 `shift = true`、`caps = true`，`shift != caps` 为 false，大小写翻转条件不成立，直接走到 `switch codeValue`，`0x61` 落在 `0x20...0x7e` 区间，返回 `0x61`。这正是「Shift 与 CapsLock 对字母大小写互相抵消」的体现。

**练习 2**：为什么「字母键」不放进最优先的主码表 `keycodeMappings`，而要单独放在 `additionalCodeMappings`？

**参考答案**：因为字母键的 keysym 取决于 Shift/CapsLock 状态（`XK_a` vs `XK_A`），必须走「字符路径」才能正确反映大小写。如果把字母键放进主码表（第 1 段优先），那么主码表会**在看到字符之前**就返回一个固定的 `XK_a`，彻底忽略大小写，导致 Shift+a 永远送小写。所以字母键必须排在字符路径之后，作为「拿不到字符时的兜底」。

**练习 3**：`flagsChanged` 事件调用 `osxKeycodeToRime` 时为什么把 `keychar` 传 `nil`？

**参考答案**：修饰键状态变化事件本身不产生可见字符（你松开 Shift 不会打出字），所以没有字符可用；同时修饰键的物理键码都在主码表 `keycodeMappings` 里，第 1 段就能直接命中。传 `nil` 是既符合事实（无字符）、又强制走码表路径的正确做法。

---

### 4.3 inferModifierKeycode：远程桌面的 keyCode=0 兜底

#### 4.3.1 概念说明

正常情况下，`flagsChanged` 事件的 `event.keyCode` 会告诉我们「是哪一个物理修饰键发生了变化」（比如 `kVK_Shift = 56`）。但有一类坑：**某些远程桌面软件**在转发修饰键事件时，会把 `keyCode` 填成 `0`（即 `kVK_ANSI_A` 的码，毫无意义）。于是 Squirrel 拿到一个「值是 0、但其实是修饰键变化」的事件，无法直接翻译。

`inferModifierKeycode` 就是为此设计的兜底：既然 `keyCode` 不可信，那就**从修饰键掩码的「变化量」反推**到底是哪个修饰键变了。

#### 4.3.2 核心流程

输入是「新旧修饰键掩码的差异」`changes`（调用方用 `symmetricDifference` 算出）。`inferModifierKeycode` 按固定优先级逐项判断：

```
函数 inferModifierKeycode(changes):
  if changes 含 .capsLock -> 返回 kVK_CapsLock
  if changes 含 .shift    -> 返回 kVK_Shift       # 注意：无法区分左右，一律取左
  if changes 含 .control  -> 返回 kVK_Control
  if changes 含 .option   -> 返回 kVK_Option
  if changes 含 .command  -> 返回 kVK_Command
  否则返回 nil
```

它只能推断出「左」修饰键（`kVK_Shift` 而非 `kVK_RightShift`），因为掩码差异里并不区分左右 Shift。这是兜底方案的固有局限，但够用——远程桌面场景下能正确触发修饰键事件，比区分左右更重要。

#### 4.3.3 源码精读

[MacOSKeyCodes.swift:77-90](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/MacOSKeyCodes.swift#L77-L90)：

```swift
static func inferModifierKeycode(from changes: NSEvent.ModifierFlags) -> UInt16? {
  if changes.contains(.capsLock)      { return UInt16(kVK_CapsLock) }
  else if changes.contains(.shift)    { return UInt16(kVK_Shift) }
  else if changes.contains(.control)  { return UInt16(kVK_Control) }
  else if changes.contains(.option)   { return UInt16(kVK_Option) }
  else if changes.contains(.command)  { return UInt16(kVK_Command) }
  return nil
}
```

它和另一个集合 `modifierKeycodes`（[MacOSKeyCodes.swift:68-75](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/MacOSKeyCodes.swift#L68-L75)）配合使用。`modifierKeycodes` 列出了所有「合法的修饰键物理键码」（含左右两套与 fn 键），调用方先用它判断 `keyCode` 是否可信：

```swift
static let modifierKeycodes: Set<UInt16> = [
  UInt16(kVK_Shift), UInt16(kVK_RightShift),
  UInt16(kVK_CapsLock),
  UInt16(kVK_Control), UInt16(kVK_RightControl),
  UInt16(kVK_Option), UInt16(kVK_RightOption),
  UInt16(kVK_Command), UInt16(kVK_RightCommand),
  UInt16(kVK_Function)
]
```

完整的兜底逻辑在 `flagsChanged` 分支（[SquirrelInputController.swift:60-70](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L60-L70)）：

```swift
// Some remote desktop tools send flagsChanged with keyCode 0; infer the real modifier key when needed.
var keyCode = event.keyCode
if !SquirrelKeycode.modifierKeycodes.contains(keyCode) {
  guard let inferred = SquirrelKeycode.inferModifierKeycode(from: changes) else {
    lastModifiers = modifiers
    rimeUpdate()
    handled = true
    break
  }
  keyCode = inferred
}
```

流程是：先用 `modifierKeycodes.contains` 验真；不可信才调 `inferModifierKeycode` 反推；连反推都失败（`changes` 为空，理论上不该发生）就放弃这次按键、只刷新状态。

#### 4.3.4 代码实践

**实践目标**：理解「键码验真 → 推断兜底」这条防御链的两个判定条件。

**操作步骤**（源码阅读型实践）：

1. 设想一个远程桌面发来的 `flagsChanged` 事件：`event.keyCode = 0`，`modifiers` 从 `{}` 变成 `{ .shift }`。
2. 第一步验真：`modifierKeycodes.contains(0)` 为 false（0 是 `kVK_ANSI_A`，不在修饰键集合里），进入兜底分支。
3. 第二步推断：`changes = {}.symmetricDifference({.shift}) = {.shift}`，`inferModifierKeycode` 返回 `kVK_Shift = 56`。
4. 于是 `keyCode` 被修正为 56，后续 `osxKeycodeToRime(keycode: 56, keychar: nil, ...)` 在主码表里命中 `kVK_Shift → XK_Shift_L`，正确翻译出 Shift 按键。
5. 再设想一个正常本地事件：`event.keyCode = 56`（左 Shift）。`modifierKeycodes.contains(56)` 为 true，**跳过**兜底，直接用真实键码。

**需要观察的现象**：验真集合 `modifierKeycodes` 是兜底逻辑的「开关」——只有 `keyCode` 不在集合里时才触发推断。

**预期结果**：远程桌面的 `keyCode=0` 被修正为真实修饰键码；本地正常事件不受影响，零开销。

#### 4.3.5 小练习与答案

**练习 1**：`inferModifierKeycode` 能区分「左 Shift」和「右 Shift」吗？为什么？

**参考答案**：不能。它的输入 `changes` 是 `NSEvent.ModifierFlags` 的差异，而 `.shift` 这个标志位**不区分左右**——左 Shift 和右 Shift 都会置 `.shift`。所以推断只能返回统一的 `kVK_Shift`（左）。要区分左右，必须依赖事件本身携带的正确 `keyCode`（`kVK_Shift` vs `kVK_RightShift`），而这正是远程桌面场景里缺失的。

**练习 2**：为什么要先 `modifierKeycodes.contains(keyCode)` 验真，再决定是否推断？直接每次都推断不行吗？

**参考答案**：因为本地正常事件里 `keyCode` 是可靠的，且能区分左右修饰键、能识别 `kVK_Function`（fn 键，掩码差异里没有对应项）。每次都推断会丢失这些信息（统一退化成「左」、且漏掉 fn）。所以策略是「信不过才推断」：先验真，可信就直接用真实键码，不可信才兜底。

---

### 4.4 keycodeMappings / additionalCodeMappings：两张码表的组织

#### 4.4.1 概念说明

前面三个模块都在讲「算法」，这个模块讲「数据」。`SquirrelKeycode` 内部维护两张「Apple 物理键码 → X11 keysym」的字典，它们分工明确、优先级不同，是整个翻译层的数据基石。

- **`keycodeMappings`（主码表，优先）**：收录**与字符无关、必须按物理位置翻译**的键。包括修饰键、特殊键（Return/Space/Tab/Delete/Esc）、功能键（F1–F20）、方向键、翻页键、小键盘、ISO/JIS 国际键。
- **`additionalCodeMappings`（备用码表，兜底）**：收录**可打印 ASCII 键**——26 个字母、10 个数字、常见标点。它们只在「字符路径走不通」时才用。

为什么要分两张表、且让字母键排在字符路径之后？核心原因在 4.2 已经讲过：**字母 keysym 取决于大小写，必须由字符路径决定，不能被物理键码抢先固定**。把它们放进低优先级的备用码表，是对「字符优先」这一翻译策略的数据层体现。

#### 4.4.2 核心流程

两张表在 `osxKeycodeToRime` 里的调用顺序，构成了一个清晰的优先级漏斗：

```
按键进来
   │
   ├─ 第 1 段：查 keycodeMappings（主码表）   ← 功能/方向/小键盘/修饰键在此命中
   │     命中？→ 返回
   │
   ├─ 第 2 段：字符路径（大小写感知）          ← 字母/数字/标点的正常路径
   │     命中？→ 返回
   │
   ├─ 第 3 段：查 additionalCodeMappings（备用）← 字母/数字/标点的兜底
   │     命中？→ 返回
   │
   └─ 第 4 段：返回 XK_VoidSymbol
```

漏斗的每一层都更「便宜」或更「确定」：能按物理键码确定的（功能键）最先确定；需要看字符的（字母）随后；都失败才报无效。

#### 4.4.3 源码精读

主码表 [MacOSKeyCodes.swift:92-173](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/MacOSKeyCodes.swift#L92-L173)，按用途分组，节选修饰键与功能键两组：

```swift
private static let keycodeMappings: [Int: Int32] = [
  // modifiers
  kVK_CapsLock: XK_Caps_Lock,
  kVK_Command: XK_Super_L,       // XK_Meta_L?
  kVK_RightCommand: XK_Super_R,
  kVK_Control: XK_Control_L,
  kVK_RightControl: XK_Control_R,
  kVK_Function: XK_Hyper_L,
  kVK_Option: XK_Alt_L,
  kVK_RightOption: XK_Alt_R,
  kVK_Shift: XK_Shift_L,
  kVK_RightShift: XK_Shift_R,
  // special
  kVK_Delete: XK_BackSpace,
  kVK_Escape: XK_Escape,
  kVK_ForwardDelete: XK_Delete,
  ...
  kVK_Space: XK_space,
  kVK_Tab: XK_Tab,
  // function / cursor / keypad / JIS ...
]
```

备用码表 [MacOSKeyCodes.swift:175-231](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/MacOSKeyCodes.swift#L175-L231)，节选数字与字母两组：

```swift
private static let additionalCodeMappings: [Int: Int32] = [
  // numbers
  kVK_ANSI_0: XK_0, ... kVK_ANSI_9: XK_9,
  // punct
  kVK_ANSI_RightBracket: XK_bracketright,
  kVK_ANSI_LeftBracket: XK_bracketleft,
  ...
  // letters
  kVK_ANSI_A: XK_a, kVK_ANSI_B: XK_b, ... kVK_ANSI_Z: XK_z
]
```

注意几个细节：

- 两张表都声明为 `private`，外部只能通过 `osxKeycodeToRime` 间接使用，保证优先级不被绕过。
- 主码表里 Command 键映射到 `XK_Super_L`（注释里还留了 `// XK_Meta_L?` 的历史犹豫），这与 4.1 里 `.command → kSuperMask` 的命名映射一脉相承。
- `kVK_Delete`（退格键）映射到 `XK_BackSpace`，而 `kVK_ForwardDelete` 映射到 `XK_Delete`——macOS 的「Delete」是退格，X11 的「Delete」是向前删除，命名正好错位，这里做了正确对齐。
- 备用码表里字母一律映射到**小写** `XK_a`，再次印证「这张表只在没有字符时兜底，大小写不归它管」。

#### 4.4.4 代码实践

**实践目标**：通过「假设把字母键挪进主码表」的思想实验，亲身体会两张表分层的必要性。

**操作步骤**（源码阅读 + 思想实验）：

1. 在 [MacOSKeyCodes.swift:33-66](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/MacOSKeyCodes.swift#L33-L66) 的 `osxKeycodeToRime` 里，确认主码表查询（第 34 行）排在字符路径（第 38 行）之前。
2. 思想实验：假设把 `kVK_ANSI_A: XK_a` 这一条从 `additionalCodeMappings` 挪到 `keycodeMappings` 最前面。
3. 推演「Shift + a」会变成什么：第 1 段 `keycodeMappings[0]` 立刻命中并返回 `XK_a`（97），**根本不会**进入第 2 段的字符路径，大小写翻转逻辑被彻底绕过。
4. 后果：Shift+a 永远送给引擎 `XK_a`（小写）+ `kShiftMask`，引擎会以为打的是小写 a，输入方案里靠「大写 A」触发的规则全部失效。

**需要观察的现象**：把字母键提前到主码表，会让「字符路径的大小写处理」整段失效。

**预期结果**：验证「字母键必须放在字符路径之后」这一设计约束——这不是代码风格，而是正确性要求。

> 说明：本实践是「阅读 + 推演」型，不需要真正修改源码（本讲义禁止改源码）。如果你想本地验证，可在一份拷贝上实验，但不要提交到仓库。

#### 4.4.5 小练习与答案

**练习 1**：macOS 的退格键 `kVK_Delete` 映射到哪个 keysym？为什么不是 `XK_Delete`？

**参考答案**：映射到 `XK_BackSpace`。因为 macOS 键盘上标「Delete」的那个键，功能上是**向左退格**（BackSpace）；而 X11 的 `XK_Delete` 是**向前删除**（Delete）。Squirrel 在主码表里把 `kVK_Delete → XK_BackSpace`、`kVK_ForwardDelete → XK_Delete`，做了正确的语义对齐，避免引擎收到相反的删除方向。

**练习 2**：备用码表 `additionalCodeMappings` 里 `kVK_ANSI_A` 映射到 `XK_a`（小写）。既然如此，Shift+a 时为什么不直接查这张表？

**参考答案**：因为 Shift+a 时字符路径（第 2 段）会先把 `"a"` 翻成 `XK_A`（大写）并返回，根本轮不到第 3 段的备用码表。备用码表只在「字符路径走不通」时（如 `flagsChanged` 传 `keychar: nil`）才被用到，那时确实会返回小写 `XK_a`，但那种场景本就不涉及大小写（修饰键事件）。

**练习 3**：如果有一个全新的 Apple 物理键码既不在主码表、也不在备用码表，且事件没带可用字符，`osxKeycodeToRime` 会返回什么？

**参考答案**：返回 `XK_VoidSymbol`（一个表示「无效/空洞」的特殊 keysym）。它是个非零的大数值，调用方 `processKey` 会把它原样传给 `rimeAPI.process_key`，引擎通常会忽略这种无效键。

---

## 5. 综合实践

把本讲四个模块串起来，做一次「**完整按键翻译链路走查**」。这是把知识连成线的小任务。

**任务**：选择下面三个按键场景，从 `NSEvent` 的原始字段出发，一路写到 `rimeAPI.process_key(session, keycode, modifier)` 的最终实参，并用一句话说明每个场景「走了哪一段翻译路径」。

**场景 A — 普通字母 `a`（无修饰键）**

- 起点：`keyCode = 0`，`modifiers = {}`，`charactersIgnoringModifiers = "a"`。
- 翻译路径：`keyDown` 分支 → `osxKeycodeToRime`：主码表未命中 → 字符路径，`shift != caps` 为 false 不翻转，`0x61` 落在 `0x20...0x7e` 返回 `0x61`。
- 修饰键：`osxModifiersToRime({}) = 0`。
- 终点：`process_key(session, 0x61, 0)`。
- 一句话：走「字符路径」，大小写不翻转。

**场景 B — `Shift + a`**

- 即 4.2.4 的主实践：`process_key(session, 0x41, kShiftMask)`，走「字符路径 + 大小写翻转」。

**场景 C — 远程桌面里的「按下 Shift」**

- 起点：`flagsChanged` 事件，`event.keyCode = 0`（被远程软件填错），`modifiers` 从 `{}` 变 `{ .shift }`。
- 翻译路径：`flagsChanged` 分支 → `modifierKeycodes.contains(0)` 为 false → `inferModifierKeycode({.shift})` 返回 `kVK_Shift = 56` → `osxKeycodeToRime(56, nil, ...)`：主码表命中 `kVK_Shift → XK_Shift_L`。
- 修饰键：`osxModifiersToRime({.shift}) = kShiftMask`（再按 4.1 的方式按位或）。
- 终点：`process_key(session, XK_Shift_L, kShiftMask)`。
- 一句话：走「验真失败 → 推断兜底 → 主码表」三连。

**完成后**，你应该能用一张表概括三种路径的触发条件：

| 场景 | 触发的翻译路径 | 关键函数/数据 |
| --- | --- | --- |
| 可见字符按键（带字符） | 字符路径（大小写感知） | `osxKeycodeToRime` 第 2 段 |
| 功能/方向/小键盘/修饰键 | 主码表 | `keycodeMappings` |
| 无字符兜底（字母/数字/标点） | 备用码表 | `additionalCodeMappings` |
| 远程桌面 keyCode=0 | 推断兜底 | `inferModifierKeycode` + `modifierKeycodes` |

## 6. 本讲小结

- Squirrel 在 macOS 与 librime 之间架了一层 `SquirrelKeycode` 翻译，把「Apple 物理键码 + NSEvent 字符 + 修饰键掩码」翻译成「X11 keysym + Rime 修饰键掩码」。
- `osxModifiersToRime` 把 macOS 的 `ModifierFlags` 逐位搬运成 Rime 的 `k*Mask`，注意 `.option→kAltMask`、`.command→kSuperMask`、`.capsLock→kLockMask` 的命名映射。
- `osxKeycodeToRime` 采用「主码表 → 字符路径 → 备用码表 → VoidSymbol」四段优先级；字母键的大小写由 `shift != caps`（异或）决定是否翻转。
- 两张码表分层是正确性要求而非风格：字母键**必须**排在字符路径之后（放进 `additionalCodeMappings`），否则大小写处理会被物理键码抢先固定而失效。
- `inferModifierKeycode` + `modifierKeycodes` 组成「键码验真 → 推断兜底」防御链，专门对付远程桌面软件把 `flagsChanged` 的 `keyCode` 填成 0 的坑。
- 至此，从 `NSEvent` 进门到 `rimeAPI.process_key` 调用的「前端→引擎分界点」已经完整打通，下一讲将进入「引擎处理完后，前端如何取回结果」。

## 7. 下一步学习建议

本讲讲完了「按键如何送进引擎」。下一步建议学习 **u2-l6 rimeUpdate 数据流**，它回答对偶的问题：**按键送进引擎之后，前端如何把引擎的处理结果（提交文本、状态、上下文、候选词）取回来**。你会看到 `get_commit` / `get_status` / `get_context` 的三段式消费，以及对应的 `free_*` 配对释放。

建议继续阅读的源码：

- `sources/SquirrelInputController.swift` 的 `rimeUpdate()` 与 `rimeConsumeCommittedText()`（取回引擎结果的主入口）。
- `sources/BridgingFunctions.swift`（C 结构的初始化与字符串所有权约定，u2-l6 与 u5-l4 会深入）。
