# 键盘事件处理主循环

## 1. 本讲目标

本讲精读 Squirrel 输入法最核心的一个方法：`SquirrelInputController.handle(_:client:)`。它是 macOS（通过 InputMethodKit）每收到一个键盘事件就回调进入的「主循环」，是「前端收键盘」与「引擎做转换」之间真正的交汇点。

学完本讲你应该能够：

1. 说清楚 `handle` 的整体结构：它只处理哪两类事件、按什么顺序分发。
2. 解释 `flagsChanged` 分支为什么要「先处理释放、再处理按下」，以及 capslock 为什么要在送入 librime 前「回拨」一次锁状态位。
3. 解释 `keyDown` 分支为什么要把带 `Command` 修饰的按键直接放行给宿主应用。
4. 准确说出返回 `true` / `false` 对一个按键事件的「吞掉 / 透传」语义，并理解 `handled` 这个布尔值是从哪里来的。

本讲只聚焦「事件如何进入、如何分流、如何决定吞还是放」这一层；至于「按键码具体怎么从 macOS 映射到 Rime」是下一讲 u2-l5 的主题，本讲只在用到时点到为止。

## 2. 前置知识

阅读本讲前，请确认你已经理解下面几个概念（它们在 u1-l5 与 u2-l3 中已建立）：

- **IMK 的事件回调契约**：`IMKServer` 把键盘事件交给 `IMKInputController`，入口就是 `handle(_:client:)`。系统只回调你在 `recognizedEvents(_:)` 里登记过的事件类型。
- **`client` 是目标应用**：一个 `weak` 的 `IMKTextInput`，代表当前正在接收文字的宿主文本框。它生命周期不属于输入法，所以用弱引用，每次事件都要「刷新 + 守卫」（u2-l3 讲过的 `?=` 运算符）。
- **marked text 与 commit text**：`setMarkedText` 画临时预编辑、`insertText` 最终上屏（u1-l5）。
- **librime 的 `process_key`**：前端把「一个按键码 + 一组修饰键掩码」喂给引擎，引擎返回 Bool 表示「我有没有消化这个键」。

本讲会用到三个 Cocoa / Swift 的小知识点，先简单铺垫：

- **`NSEvent.modifierFlags`**：一个位掩码（`OptionSet`），用 `.shift`、`.control`、`.option`、`.command`、`.capsLock` 等位表示「此刻按下了哪些修饰键」。
- **`symmetricDifference(_:)`**：`OptionSet` 的方法，返回「两个集合各自独有」的位，也就是「发生了变化的」那些修饰键。这是 Squirrel 检测「谁被按下、谁被松开」的关键。
- **`NSEvent.EventType`**：macOS 把「普通按键」归为 `.keyDown`，而**修饰键的按下 / 松开**并不走 keyDown，而是单独走 `.flagsChanged` 事件。这是理解整个 `handle` 为什么要分两个大分支的前提。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件展开，再借用两个辅助文件解释细节：

| 文件 | 作用 |
| --- | --- |
| [sources/SquirrelInputController.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift) | 输入控制器，本讲主角。`handle(_:client:)`、`recognizedEvents(_:)`、`processKey(_:modifiers:)` 都在这里。 |
| [sources/MacOSKeyCodes.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/MacOSKeyCodes.swift) | macOS → Rime 的按键 / 修饰键映射工具。本讲只用到其中与修饰键判定相关的几个函数（完整映射留到 u2-l5）。 |
| [sources/BridgingFunctions.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/BridgingFunctions.swift) | Swift / C 桥接工具，定义了 `?=` 运算符等本讲会用到的约定（完整桥接约定在 u5-l4）。 |

## 4. 核心概念与源码讲解

### 4.1 事件类型分发与 session 自愈

#### 4.1.1 概念说明

`handle(_:client:)` 是输入法的「大门」。每当用户在某个文本框里敲键，macOS 都会带着一个 `NSEvent` 和一个 `client`（目标应用代理）回调进来。Squirrel 要在这一个方法里完成三件事：

1. **判断这是哪一类事件**，决定走哪条处理路径；
2. **维护好和引擎的会话**（session），确保引擎那头「记得」当前用户的状态；
3. **决定这个事件是「自己吃掉」还是「放给宿主应用」**，并用返回值告诉系统。

这里有一个容易忽略的前提：系统**只会**把你在 `recognizedEvents(_:)` 中登记的事件类型送进来。Squirrel 只登记了两类：

```swift
override func recognizedEvents(_ sender: Any!) -> Int {
  return Int(NSEvent.EventTypeMask.Element(arrayLiteral: .keyDown, .flagsChanged).rawValue)
}
```

也就是说，`handle` 实际只会见到 `.keyDown` 与 `.flagsChanged`，其余事件（鼠标、滚动等）根本不会进入这个方法。

> 参考：[sources/SquirrelInputController.swift:L163-L165](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L163-L165) —— 登记只关心 `.keyDown` 与 `.flagsChanged`。

#### 4.1.2 核心流程

`handle` 的整体结构可以画成下面这条流水线：

```text
NSEvent 进门
  │
  ├─ 1. 事件本身判空（guard event）
  ├─ 2. 取当前修饰键 modifiers，并算出「变化量」changes
  ├─ 3. session 自愈：session 失效就重建
  ├─ 4. 刷新弱引用 client；若换了 App，重载应用级选项
  │
  └─ switch event.type
        ├─ .flagsChanged  → 修饰键分支（4.2）
        ├─ .keyDown       → 普通按键分支（4.3）
        └─ default        → 兜底，什么都不做
  │
  └─ return handled   （true=吞掉，false=透传，见 4.4）
```

「session 自愈」是这条流水线里很实在的一道保险。在 u2-l3 里我们知道：一个 controller 对应一个 librime session，`session == 0` 表示「当前没有有效会话」。但在某些异常情况（比如引擎内部把会话清掉了），`session` 可能变成无效。`handle` 每次进门都先体检一次：

```swift
if session == 0 || !rimeAPI.find_session(session) {
  createSession()
  if session == 0 {
    return false
  }
}
```

如果会话不在了，就立刻 `createSession()` 重建一个；如果连重建都失败（`session` 仍是 0），那就直接 `return false`，把这个事件透传出去——因为已经没有引擎会话可以处理它了，硬撑下去只会崩。

> 参考：[sources/SquirrelInputController.swift:L40-L45](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L40-L45) —— session 失效即重建的自愈逻辑。

体检之后，还有两件每次都要做的小事：

```swift
self.client ?= sender as? IMKTextInput
if let app = client?.bundleIdentifier(), currentApp != app {
  currentApp = app
  updateAppOptions()
}
```

- `self.client ?= sender as? IMKTextInput`：用项目自定义的 `?=` 运算符（仅当右侧非 nil 才赋值）刷新弱引用 `client`。因为 `client` 是 `weak`，随时可能被释放，每次事件都要重新抓一次。
- 如果发现目标应用的 bundle id 变了（用户切到了另一个 App 的文本框），就调用 `updateAppOptions()`，把 `squirrel.yaml` 里 `app_options` 针对该应用的选项（如 `ascii_mode`、`no_inline`）重新下发到引擎。

> 参考：[sources/SquirrelInputController.swift:L47-L51](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L47-L51) —— 刷新 client 与按 App 重载选项。
>
> `?=` 的定义见 [sources/BridgingFunctions.swift:L44-L56](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/BridgingFunctions.swift#L44-L56)。

#### 4.1.3 源码精读

`handle` 的方法签名与开头的「变化量」计算：

```swift
// swiftlint:disable:next cyclomatic_complexity
override func handle(_ event: NSEvent!, client sender: Any!) -> Bool {
  guard let event = event else { return false }
  let modifiers = event.modifierFlags
  let changes = lastModifiers.symmetricDifference(modifiers)

  // Return true to consume the key event; return false to pass it to the client app.
  var handled = false
```

几个要点：

- 方法上方的 `// swiftlint:disable:next cyclomatic_complexity` 说明这段代码分支很多、圈复杂度高，作者主动告诉 linter「这里复杂是有道理的，别报警」。这也是为什么本讲要把它拆成几个最小模块来读。
- `changes = lastModifiers.symmetricDifference(modifiers)`：`lastModifiers` 是上一次事件后记住的修饰键状态。两者做对称差，得到的正是「这一次相比上一次，哪些修饰键位发生了翻转」。这是后面判断「按下还是松开」的总依据。
- 第 37 行那句英文注释是整篇的纲领：**返回 `true` 表示吞掉事件，返回 `false` 表示放给宿主应用**。`handled` 这个局部变量会在各分支里被改写，最终在第 123 行 `return handled`。

> 参考：[sources/SquirrelInputController.swift:L31-L38](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L31-L38) —— `handle` 签名、变化量计算与吞/放纲领。

随后是 `switch event.type` 的大分流：

```swift
switch event.type {
case .flagsChanged:
  // ... 修饰键分支（4.2）
case .keyDown:
  // ... 普通按键分支（4.3）
default:
  break
}
```

> 参考：[sources/SquirrelInputController.swift:L53-L121](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L53-L121) —— 三路分发：flagsChanged / keyDown / default。

注意 `default: break` 这一路。因为 `recognizedEvents` 只登记了两类事件，正常运行里几乎走不到 `default`；它只是 Swift `switch` 穷尽性的一个兜底，保持代码健壮。

#### 4.1.4 代码实践

**实践目标**：用源码阅读的方式，把「事件进门 → 分发 → 返回」这条骨架走一遍，确认你对结构的理解。

**操作步骤**：

1. 打开 [sources/SquirrelInputController.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift)，定位 `handle(_:client:)`（约第 32 行）。
2. 找到 `recognizedEvents(_:)`，确认它只登记了 `.keyDown` 与 `.flagsChanged`。
3. 在 `handle` 里数出三个关键节点：① session 自愈（约第 40–45 行）；② client / App 刷新（约第 47–51 行）；③ `switch event.type` 分发（约第 53 行）。
4. 找到最后的 `return handled`（约第 123 行），回顾它在三个分支里分别是什么值。

**需要观察的现象 / 预期结果**：

- `handled` 在方法开头初始化为 `false`，意味着「默认放行」。只有 `.flagsChanged` 的某个早返回路径和 `.keyDown` 真正把键送进引擎后，才可能变成 `true`。
- `default` 分支不改变 `handled`，所以任何「意料之外」的事件类型都会以 `false`（透传）结束——这是一个安全的默认行为。

**待本地验证**：若你手头有 macOS 并装好 Squirrel，可在 `handle` 开头加一行 `print("[handle] type=\(event.type)")`，编译安装后在「系统设置 → 键盘 → 输入法」里选中 Squirrel，随便敲几个键，到「控制台 (Console.app)」过滤 Squirrel 进程，应能看到一连串 `type=keyDown`，而按 Shift/Cmd 时会夹杂 `type=flagsChanged`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `handle` 开头要做 `guard let event = event else { return false }`？返回 `false` 而不是 `true` 有什么讲究？

> **参考答案**：`event` 是 IMK 传进来的隐式解包可选值，理论上可能为 nil。守卫之后直接 `return false` 表示「输入法不处理这个空事件，把它透传给宿主应用」——比起 `return true`（吞掉）更安全，因为吞掉一个空事件相当于让用户的按键凭空消失。

**练习 2**：如果某个第三方工具向 Squirrel 注入了一个 `.mouseMoved` 事件，`handle` 会怎么处理？

> **参考答案**：实际上不会发生——`recognizedEvents` 只登记了 `.keyDown` 与 `.flagsChanged`，系统不会把鼠标事件回调给 `handle`。即便真的进来，也会落到 `default: break`，`handled` 保持 `false`，事件被透传。

---

### 4.2 flagsChanged：capslock 与释放优先

#### 4.2.1 概念说明

这是 `handle` 里最绕的一个分支，原因是 macOS 对修饰键的处理方式与 librime 的预期并不完全一致：

- **macOS**：按 Shift、Ctrl、Option、Command、CapsLock，都不会产生 `.keyDown`，而是产生一个 `.flagsChanged` 事件，事件里只告诉你「现在的修饰键状态变成什么样了」，不直接告诉你「具体是哪个键被按下 / 松开」。
- **librime**：仍然希望像接收普通按键一样，收到「某个修饰键的 keycode + 它的修饰掩码 + 是否是松开（release）」。

所以 Squirrel 必须做两件翻译工作：

1. **从「状态变化」反推出「哪个键变了、是按下还是松开」**，并补上一个 `kReleaseMask` 位来标记松开；
2. **修正 capslock 的时序**：librime 期望在「锁状态尚未翻转」时收到 `XK_Caps_Lock`，而 macOS 的 flagsChanged 到达时锁状态已经翻转了，需要把这一位「回拨」回去。

#### 4.2.2 核心流程

flagsChanged 分支的执行流程：

```text
.flagsChanged 进门
  │
  ├─ 若 lastModifiers == modifiers（无变化）→ 吞掉，结束
  ├─ 算 rimeModifiers（把 macOS 掩码翻译成 Rime 掩码）
  ├─ 取 keyCode；若不是已知修饰键码（如远程桌面发来 0），则按 changes 推断
  ├─ 若 capslock 变了：
  │     rimeModifiers 异或掉 kLockMask（回拨锁状态）
  │     把 XK_Caps_Lock 送进引擎
  ├─ 遍历 {shift, control, option, command} 中「变了」的位：
  │     松开 → insert 到 buffer 头部（带 kReleaseMask）
  │     按下 → append 到 buffer 尾部
  ├─ 顺序处理 buffer（先释放、后按下）
  ├─ 记住 lastModifiers = modifiers
  └─ rimeUpdate()  刷新面板
```

「先释放、后按下」是用一个 `buffer` 数组实现的：松开的键 `insert(..., at: 0)` 放到队首，按下的键 `append` 放到队尾，然后从头到尾依次送进引擎。

capslock 的「回拨」用一次按位异或完成。设当前 `modifiers` 已经包含 capslock（即锁状态已翻转），则 `osxModifiersToRime` 会把 `kLockMask` 置位；再异或一次 `kLockMask` 就把它清掉：

\[
\text{rimeModifiers} \;\oplus\; \text{kLockMask}
\]

效果是把锁位「还原」到翻转前的状态，匹配 librime 对 `XK_Caps_Lock` 的时序预期。

#### 4.2.3 源码精读

flagsChanged 分支开头先做一个去重早返回：

```swift
case .flagsChanged:
  if lastModifiers == modifiers {
    handled = true
    break
  }
```

如果这次 flagsChanged 的修饰键状态和上次一模一样（冗余 / 合成事件），就直接 `handled = true` 吞掉，跳过后续所有处理。这是「无变化就不折腾」。

> 参考：[sources/SquirrelInputController.swift:L54-L58](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L54-L58) —— flagsChanged 去重早返回。

接下来把 macOS 掩码翻译成 Rime 掩码，并处理「远程桌面 keyCode 异常」的兜底：

```swift
var rimeModifiers: UInt32 = SquirrelKeycode.osxModifiersToRime(modifiers: modifiers)
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

- `modifierKeycodes` 是一张「已知修饰键硬件码」的集合（左右 Shift / Ctrl / Option / Command、CapsLock、Fn）。如果事件里的 `keyCode` 不在这张表里（最典型就是某些远程桌面工具发来的 `0`），就根据 `changes` 反推到底是哪个键变了；推不出来就只能作罢，更新状态后吞掉。

> 参考：[sources/SquirrelInputController.swift:L59-L70](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L59-L70) —— 掩码翻译与 keyCode 推断兜底。
>
> `modifierKeycodes` 与 `inferModifierKeycode` 的定义在 [sources/MacOSKeyCodes.swift:L68-L90](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/MacOSKeyCodes.swift#L68-L90)。

接着是 capslock 的「回拨」：

```swift
if changes.contains(.capsLock) {
  // Rime expects XK_Caps_Lock before the lock mask changes; NSFlagsChanged has already applied it.
  rimeModifiers ^= kLockMask.rawValue
  _ = processKey(rimeKeycode, modifiers: rimeModifiers)
}
```

注释写得很清楚：Rime 期望在「锁掩码变化之前」收到 `XK_Caps_Lock`，而 `NSFlagsChanged` 到达时锁状态已经被应用了，所以用 `^=`（异或）把 `kLockMask` 这一位清掉，让引擎看到「翻转前」的状态。

> 参考：[sources/SquirrelInputController.swift:L73-L77](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L73-L77) —— capslock 时序修正。

然后是本分支的「重头戏」——释放优先的缓冲区：

```swift
// Process releases first because some modifier releases arrive with the next keydown.
var buffer = [(keycode: UInt32, modifier: UInt32)]()
for flag in [NSEvent.ModifierFlags.shift, .control, .option, .command] where changes.contains(flag) {
  if modifiers.contains(flag) {
    buffer.append((keycode: rimeKeycode, modifier: rimeModifiers))
  } else {
    buffer.insert((keycode: rimeKeycode, modifier: rimeModifiers | kReleaseMask.rawValue), at: 0)
  }
}
for (keycode, modifier) in buffer {
  _ = processKey(keycode, modifiers: modifier)
}
```

读法：

- 只遍历 `shift / control / option / command` 这四个「可能作为普通修饰」的位（capslock 已单独处理），且只看「变了」的位。
- 若 `modifiers.contains(flag)` 为真 → 这是**按下**，`append` 到队尾，不带 release 位。
- 否则 → 这是**松开**，`insert` 到队首（`at: 0`），并额外或上 `kReleaseMask.rawValue` 告诉引擎「这是松开」。
- 最后顺序遍历 `buffer`，由于所有松开都被塞到了队首、所有按下都在队尾，所以**先送释放、再送按下**。

> 参考：[sources/SquirrelInputController.swift:L79-L90](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L79-L90) —— 释放优先的缓冲区。

最后收尾：

```swift
lastModifiers = modifiers
rimeUpdate()
```

更新「上次状态」，并调用 `rimeUpdate()` 把引擎的最新候选 / 预编辑刷到面板（`rimeUpdate` 的细节是 u2-l6 的内容）。注意：这一支里 `handled` 在主路径上**没有被置 true**，所以一次「真正发生变化的」flagsChanged 最终会以 `false`（透传）返回——修饰键状态变化一般应让宿主应用也知道。

> 参考：[sources/SquirrelInputController.swift:L92-L93](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L92-L93) —— 更新状态并刷新面板。

#### 4.2.4 代码实践

**实践目标**：亲手模拟一次「Shift 松开」的 flagsChanged，验证「释放优先」缓冲区的构造顺序。

**操作步骤**：

1. 假设上一次事件后 `lastModifiers = [.shift]`（Shift 还按着）。
2. 现在用户松开 Shift，macOS 送来一个 flagsChanged，`modifiers = []`（空），于是 `changes = symmetricDifference([.shift]) = [.shift]`。
3. 走进 `for flag in [.shift, .control, .option, .command]`：只有 `.shift` 在 `changes` 里。
4. 判断 `modifiers.contains(.shift)` → `false` → 走 `else` 分支：`buffer.insert((keycode, rimeModifiers | kReleaseMask), at: 0)`，即得到一个带 release 位的条目。
5. 顺序处理 buffer，于是 `processKey` 收到一个**带 `kReleaseMask` 的 Shift 松开事件**。

**需要观察的现象 / 预期结果**：

- 单个修饰键变化时，buffer 里只有一项，顺序无所谓。
- 真正能体现「释放优先」的是**一次 flagsChanged 里同时有按下和松开**的边角情形：例如某合成事件让 `changes = [.shift, .control]`，其中 Shift 被松开、Ctrl 被按下。此时 Shift 的松开条目被 `insert` 到队首、Ctrl 的按下条目被 `append` 到队尾，遍历顺序就是「先送 Shift 松开、再送 Ctrl 按下」。

**待本地验证**：在 flagsChanged 分支的 `for (keycode, modifier) in buffer` 循环里加一句

```swift
print("[flag] release=\(modifier & kReleaseMask.rawValue != 0)")
```

（这是示例代码，仅供观察）然后在装好 Squirrel 的 macOS 上连按 Shift 与 Ctrl，在 Console 里观察 release 标志的先后。

#### 4.2.5 小练习与答案

**练习 1**：为什么注释说「Process releases first because some modifier releases arrive with the next keydown」？如果反过来「先按下、后释放」会有什么问题？

> **参考答案**：某些场景下，一个修饰键的松开事件会被 macOS 延迟、和紧接着的下一个 keyDown 捆绑或乱序到达。如果 Squirrel 先把「按下」送给引擎、再送「松开」，引擎看到的时序就和用户真实操作相反，可能导致组合判定错误（例如把「松开 Shift」误处理成「还在按 Shift」）。把释放统一排到前面，是为了在「同一批变化」内保证一个符合直觉的先后顺序，规避这种乱序边角。

**练习 2**：capslock 为什么需要 `rimeModifiers ^= kLockMask.rawValue`，而 Shift / Ctrl 不需要？

> **参考答案**：capslock 是「锁存型」修饰键——按一下状态翻转并保持；macOS 的 flagsChanged 到达时 `.capsLock` 已经是翻转后的新状态。而 librime 期望在锁状态**翻转之前**收到 `XK_Caps_Lock`。普通修饰键（Shift/Ctrl）是「按住才有效」的非锁存键，没有这个时序错位，所以无需修正。capslock 通过异或清掉 `kLockMask`，把状态「回拨」到翻转前，正好对齐 librime 的预期。

---

### 4.3 keyDown：放行 Command 快捷键

#### 4.3.1 概念说明

`.keyDown` 分支处理的是「真正的字符按键」：字母、数字、空格、回车、退格……这些才是输入法「真正要拿去做中文转换」的内容。但有一类按键 Squirrel **故意不碰**：带 `Command` 修饰键的快捷键，也就是 ⌘C、⌘V、⌘Tab、⌘S 这一类。

原因很简单：Command 快捷键属于**操作系统级 / 应用级**的操作（复制、粘贴、保存、切换窗口），它们和「输入中文」是两条互不相干的链路。如果输入法把 ⌘C 吞掉自己去处理，宿主应用的「复制」就会失灵。所以 Squirrel 在 keyDown 一进门就先放行所有 Command 组合键。

#### 4.3.2 核心流程

keyDown 分支的流程：

```text
.keyDown 进门
  │
  ├─ 若带 .command 修饰 → 直接 break（handled 保持 false，透传）
  ├─ 取 keyCode 与 keyChars（处理 shift / capslock 对字符的影响）
  ├─ 用 osxKeycodeToRime 把按键翻译成 Rime keycode
  ├─ 若 rimeKeycode != 0（不是无效键）：
  │     rimeModifiers = osxModifiersToRime(modifiers)
  │     handled = processKey(rimeKeycode, modifiers: rimeModifiers)
  │     rimeUpdate()
  └─ （否则 handled 保持 false，透传）
```

注意第 96–99 行：

```swift
case .keyDown:
  // Let client apps handle Command shortcuts.
  if modifiers.contains(.command) {
    break
  }
```

`break` 跳出 `switch`，落到方法末尾 `return handled`，而 `handled` 仍是初始值 `false`——于是这个带 Command 的按键被**透传**给宿主应用。注释 `Let client apps handle Command shortcuts` 一句话点明意图。

> 参考：[sources/SquirrelInputController.swift:L95-L99](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L95-L99) —— 放行 Command 快捷键。

#### 4.3.3 源码精读

放行 Command 之后，是字符提取与映射的逻辑：

```swift
let keyCode = event.keyCode
var keyChars = event.charactersIgnoringModifiers
let capitalModifiers = modifiers.isSubset(of: [.shift, .capsLock])
if let code = keyChars?.first,
   (capitalModifiers && !code.isLetter) || (!capitalModifiers && !code.isASCII) {
  keyChars = event.characters
}
```

这一段决定到底用哪个字符去查 Rime 键码：

- 默认用 `charactersIgnoringModifiers`（忽略 Shift / CapsLock 的「基础字符」）。
- 但当 Shift / CapsLock 把一个键变成了非字母符号（比如 `1` → `!`），或者基础字符不是 ASCII 时，就改用 `event.characters`（带修饰的实际字符）。
- `capitalModifiers = modifiers.isSubset(of: [.shift, .capsLock])` 表示「修饰键只可能是 shift/capslock」，用来判断当前是否处于「可能产生大写字母」的情形。这块字符选择的细节属于 u2-l5 的范畴，本讲理解到「它在挑一个合适的字符」即可。

> 参考：[sources/SquirrelInputController.swift:L101-L107](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L101-L107) —— 字符提取与修饰键修正。

最后把按键送进引擎：

```swift
if let char = keyChars?.first {
  let rimeKeycode = SquirrelKeycode.osxKeycodeToRime(keycode: keyCode, keychar: char,
                                                     shift: modifiers.contains(.shift),
                                                     caps: modifiers.contains(.capsLock))
  if rimeKeycode != 0 {
    let rimeModifiers = SquirrelKeycode.osxModifiersToRime(modifiers: modifiers)
    handled = processKey(rimeKeycode, modifiers: rimeModifiers)
    rimeUpdate()
  }
}
```

要点：

- `osxKeycodeToRime` 把 macOS 的 `(keyCode, 字符, shift, caps)` 四元组翻译成 Rime 的 keycode；映射不出来时会返回 `XK_VoidSymbol`（即 0）。
- `if rimeKeycode != 0` 这道关卡过滤掉无效键——映射不出来的键不送引擎，`handled` 保持 `false`，透传给宿主应用。
- 真正送引擎的是 `processKey(rimeKeycode, modifiers: rimeModifiers)`，它的返回值直接赋给 `handled`。也就是说，**这个键最终「吞还是放」，由 librime 说了算**：引擎消化了（比如正在打拼音时按 `a`）就返回 `true` 吞掉；引擎不感兴趣（比如没在组词时按 `F1`）就返回 `false` 透传。

> 参考：[sources/SquirrelInputController.swift:L108-L117](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L108-L117) —— 翻译键码并送入引擎。

`processKey` 本身做的事比「调一下 `process_key`」更多，它还顺带做了面板线性 / 垂直选项同步、vim 模式退出、chord 打字缓冲等。但就本讲而言，只需记住它的**返回值**就是 librime `process_key` 的返回值，并最终成为 `handled`：

```swift
let handled = rimeAPI.process_key(session, Int32(rimeKeycode), Int32(rimeModifiers))
...
return handled
```

> 参考：[sources/SquirrelInputController.swift:L401-L423](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L401-L423) —— `processKey` 内部调用 `rimeAPI.process_key` 并返回其结果。

#### 4.3.4 代码实践

**实践目标**：亲手对比「带 Command 的按键」与「普通按键」在 keyDown 分支里的不同命运。

**操作步骤**：

1. 在 keyDown 分支第 97 行 `if modifiers.contains(.command)` **之前**加一行临时日志（示例代码）：

   ```swift
   print("[keyDown] char=\(keyChars ?? "?") cmd=\(modifiers.contains(.command))")
   ```

2. （可选，需 macOS + Squirrel）编译安装后，在一个文本框里依次按 `a`、`⌘c`、`回车`。

**需要观察的现象 / 预期结果**：

- 按 `a`：日志打印 `char=a cmd=false`，事件被送进 `processKey`；若处于中文输入态，`handled` 多半为 `true`（吞掉，自己拿去组词）。
- 按 `⌘c`：日志打印 `char=c cmd=true`，紧接着命中 `if modifiers.contains(.command)` 直接 `break`，**不会**进入 `processKey`；事件透传，宿主应用执行「复制」。
- 这正好印证：「输入法不该、也不会拦截系统 / 应用级的 Command 快捷键。」

**待本地验证**：上面第 2 步需要一台装好 Squirrel 的 macOS 机器才能真正观察到日志与「复制」生效；若仅阅读源码，可对照第 96–99 行确认 `break` 之后 `handled` 仍为 `false`。

#### 4.3.5 小练习与答案

**练习 1**：为什么放行 Command 的代码用的是 `break`，而不是 `return false`？两者效果一样吗？

> **参考答案**：在当前结构下效果一样——`break` 跳出 `switch` 后会落到方法末尾的 `return handled`，而 `handled` 此刻是初始值 `false`，所以等价于 `return false`（透传）。作者用 `break` 是为了保持「所有分支都在统一出口 `return handled` 收口」的结构，便于阅读和维护。

**练习 2**：假如用户正在打拼音（已有未上屏的编码），这时按了 `⌘c`，会发生什么？编码会丢吗？

> **参考答案**：`⌘c` 命中 `if modifiers.contains(.command) { break }`，直接透传给宿主应用执行复制，输入法这边**不调用** `processKey`，也不动 session 里的编码。所以正在输入的拼音编码不会因为这次 ⌘c 而丢失或被清空。编码的真正「收尾」发生在 deactivate（u2-l3 讲过的 `commitComposition`）等时机，与本分支无关。

---

### 4.4 返回值语义：吞掉 vs 透传

#### 4.4.1 概念说明

`handle` 的返回值 `Bool` 是输入法与系统之间的一个**协议**，理解它就理解了整个主循环的「出口」：

- **返回 `true` —— 吞掉（consume）**：告诉 IMK「这个事件我处理了，不要再传给宿主应用」。用户看到的效果是：这个按键「消失」进了输入法，没有直接落到文本框。
- **返回 `false` —— 透传（pass through）**：告诉 IMK「这个事件我没处理（或不需要独占），请照常交给宿主应用」。用户看到的效果是：这个按键表现得就像输入法不存在，比如按 `F1` 触发应用的帮助、按 `⌘c` 触发复制。

这是一个零和的选择：一个按键要么被输入法吃掉、要么到宿主应用那里，不能两头都生效。所以 Squirrel 必须谨慎地决定每一个键的归属。

#### 4.4.2 核心流程

`handled` 这个布尔值是整篇的「总线」，它在四个位置被决定：

| 位置 | `handled` 取值 | 含义 |
| --- | --- | --- |
| 方法开头 `var handled = false` | `false` | 默认放行 |
| flagsChanged 无变化早返回 | `true` | 吞掉冗余事件 |
| flagsChanged 主路径 | 维持 `false` | 让宿主应用也感知修饰键变化 |
| keyDown 命中 `.command` | 维持 `false`（`break`） | 放行 Command 快捷键 |
| keyDown 进入 `processKey` | `= processKey(...)` 的返回值 | **由 librime 决定**吞还是放 |
| keyDown 键码映射失败 (`rimeKeycode == 0`) | 维持 `false` | 放行无法识别的键 |

最后统一在第 123 行 `return handled`。

#### 4.4.3 源码精读

决定返回值的几个关键行集中在一起看：

```swift
// Return true to consume the key event; return false to pass it to the client app.
var handled = false
```

> 参考：[sources/SquirrelInputController.swift:L37-L38](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L37-L38) —— 吞/放纲领与默认放行。

flagsChanged 无变化时吞掉：

```swift
if lastModifiers == modifiers {
  handled = true
  break
}
```

> 参考：[sources/SquirrelInputController.swift:L55-L58](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L55-L58)。

keyDown 由引擎决定（注意 `handled = processKey(...)`）：

```swift
if rimeKeycode != 0 {
  let rimeModifiers = SquirrelKeycode.osxModifiersToRime(modifiers: modifiers)
  handled = processKey(rimeKeycode, modifiers: rimeModifiers)
  rimeUpdate()
}
```

> 参考：[sources/SquirrelInputController.swift:L112-L116](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L112-L116)。

统一出口：

```swift
default:
  break
}

return handled
```

> 参考：[sources/SquirrelInputController.swift:L119-L123](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L119-L123) —— 统一收口 `return handled`。

一个值得强调的设计：**绝大多数普通按键的吞/放，最终裁决权交给了 librime**。前端只负责「把键翻译好、送进去」，引擎返回 `true` 就吞、返回 `false` 就放。这种分工让「哪些键属于输入法」这件事由方案（schema）和引擎逻辑决定，而不是前端硬编码——这也是 Rime 「前端薄、引擎厚」架构的体现（见 u1-l1）。

#### 4.4.4 代码实践

**实践目标**：把 `handled` 在不同按键下的取值总结成一张表，巩固对「吞 / 放」语义的理解。

**操作步骤**：

1. 重新通读 [sources/SquirrelInputController.swift:L32-L124](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L32-L124)。
2. 针对下面 5 种按键，逐一推断 `handled` 最终是 `true` 还是 `false`，并说明依据：
   - (a) 一次「状态没变」的冗余 flagsChanged；
   - (b) 真正按下 Shift 的 flagsChanged；
   - (c) 在中文输入态下按字母 `a`；
   - (d) 按 `⌘v`；
   - (e) 按一个 `osxKeycodeToRime` 映射不出来的罕见功能键。

**预期结果**：

| 按键 | `handled` | 依据 |
| --- | --- | --- |
| (a) 冗余 flagsChanged | `true` | L55–L58 早返回 |
| (b) 按 Shift | `false` | flagsChanged 主路径不置 true，透传 |
| (c) 中文态按 `a` | `true` | librime 消化了它（`processKey` 返回 true） |
| (d) `⌘v` | `false` | L97–L99 命中 `.command` 直接 break |
| (e) 映射不出的键 | `false` | `rimeKeycode == 0`，不进 `processKey` |

**待本地验证**：(b)(c)(e) 的实际取值依赖引擎当前状态与方案配置，需在装好 Squirrel 的 macOS 上结合日志确认；(a)(d) 由前端代码直接决定，可静态推断。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `handled` 默认是 `false` 而不是 `true`？

> **参考答案**：默认 `false` 意味着「拿不准就放行」。输入法是宿主应用的「客人」，宁可让一个没被明确处理的按键落到宿主应用（用户至少还能看到反应），也不能默认吞掉（那样用户的按键会莫名消失）。这是一种「安全默认」。

**练习 2**：如果一个按键既没命中 Command 放行、又映射不出 Rime 键码（`rimeKeycode == 0`），它会怎样？

> **参考答案**：它既不会被送进 `processKey`，也不会改变 `handled`，于是以默认值 `false` 透传给宿主应用。也就是说「输入法识别不了的键，一律放行」，避免输入法成为按键黑洞。

**练习 3**：flagsChanged 主路径处理完一次真实的修饰键变化后，为什么返回 `false` 而不是 `true`？

> **参考答案**：修饰键状态变化往往需要让宿主应用也知道（比如应用要根据 Shift 调整选择行为、根据 CapsLock 调整大小写显示）。Squirrel 在 flagsChanged 主路径里没有把 `handled` 置 `true`，相当于「我也处理了，但同时也请你（宿主应用）照常处理」，把事件透传出去。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成下面这个结合「源码修改 + 行为推断」的小任务。

**任务背景**：维护者经常需要排查「为什么某个按键被输入法吃掉了 / 没被吃掉」。你的目标是给 `handle` 加一组最小日志，让每一次事件的「类型、是否带 Command、是否送进引擎、最终吞还是放」都能在日志里看到。

**操作步骤**：

1. 打开 [sources/SquirrelInputController.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift)，在 `handle` 的 `return handled`（第 123 行）之前插入一行（示例代码，仅供本地调试，勿提交）：

   ```swift
   print("[handle] type=\(event.type) cmd=\(modifiers.contains(.command)) handled=\(handled)")
   ```

2. 分别针对下面两个问题，结合日志和源码写出你的解释（这正是本讲规格里要求的两道题）：
   - **为什么 flagsChanged 分支要「先处理释放、再处理按下」？**
     对照第 79–90 行的缓冲区逻辑，结合注释「some modifier releases arrive with the next keydown」作答。
   - **为什么 keyDown 中带 Command 修饰的键要 `break` 放行给宿主应用？**
     对照第 96–99 行，从「Command 快捷键属于系统 / 应用级操作、输入法不应拦截」的角度作答。

3. （待本地验证）若在 macOS 上装好自己编译的 Squirrel，敲击 `a`、`shift+a`、`⌘c`、松开 Shift，在 Console.app 里核对日志中 `handled` 的取值，应与本讲 4.4.4 的推断表一致。

**预期结果**：你能用自己的话讲清「释放优先」的缓冲区构造（松开 `insert` 到队首、按下 `append` 到队尾）和「Command 放行」的理由，并能从日志里验证 `handled` 的实际取值。

## 6. 本讲小结

- `handle(_:client:)` 是输入法的主循环，系统只会把 `recognizedEvents` 登记过的 `.keyDown` 与 `.flagsChanged` 两类事件送进来。
- 每次进门先做 session 自愈（失效即重建）、再刷新弱引用 `client` 并在切换 App 时重载应用级选项，最后按 `event.type` 三路分发。
- `flagsChanged` 分支把「修饰键状态变化」翻译成「带按下/松开标记的按键事件」送给引擎；capslock 因为锁状态时序错位需要 `^=` 回拨 `kLockMask`，并通过「释放 insert 到队首、按下 append 到队尾」实现释放优先。
- `keyDown` 分支一进门就放行所有带 `Command` 的按键，把系统 / 应用级快捷键（复制、粘贴等）原样透传给宿主应用。
- 普通按键的「吞 / 放」最终由 librime 的 `process_key` 返回值决定；映射不出 Rime 键码的键一律透传。
- 返回值 `true` 表示吞掉、`false` 表示透传；`handled` 默认 `false`（拿不准就放行），在统一出口 `return handled` 收口。

## 7. 下一步学习建议

本讲只解决了「事件如何进门、如何分流、如何决定吞还是放」这一层，刻意把「macOS 按键码具体怎么翻译成 Rime 键码」留到了下一讲。建议接着学：

- **u2-l5 macOS 到 Rime 的按键映射**：精读 `MacOSKeyCodes.swift`，系统了解 `osxKeycodeToRime` 的字符 / 码表双路径、`osxModifiersToRime` 的掩码映射，以及 `inferModifierKeycode` 的远程桌面兜底——本讲里点到为止的几处映射，在那里会讲透。
- **u2-l6 rimeUpdate 数据流**：本讲里多次出现的 `rimeUpdate()` 是「事件处理后刷新面板」的入口，下一讲会拆解它如何依次消费 `get_commit / get_status / get_context` 三段 librime 状态。
- 之后进入 u2-l7，了解 marked / commit 文本规则与 inline 策略，把「按键 → 引擎 → 回显」整条链路补全。
