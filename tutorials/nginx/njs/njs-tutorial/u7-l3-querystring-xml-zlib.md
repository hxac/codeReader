# querystring / xml / zlib 模块

## 1. 本讲目标

本讲承接 [u7-l1（fs 模块）](u7-l1-fs-module.md)，把视角从「文件系统」扩展到另外三个外部扩展模块。学完本讲你应该能够：

- 说清 `querystring` 模块的 `parse`/`stringify`/`escape`/`unescape` 四个 API 做了什么，并看懂百分号编码（percent-encoding）在源码里的实现。
- 理解 `xml` 模块基于 libxml2 构建的 `XMLDoc`/`XMLNode`/`XMLAttr` 对象模型，掌握 `$tag`/`$tags`/`$attr`/`$attrs`/`$text` 这种独特的属性访问语法。
- 了解 `zlib` 模块如何用 DEFLATE/INFLATE 算法做同步压缩与解压，并理解它的可选依赖特性。
- 把「双引擎 = 双份代码」铁律与「可选 C 库依赖在构建期探测」这两个贯穿性机制彻底看明白，并能据此判断「改一处行为要同步改哪些地方」。

---

## 2. 前置知识

本讲默认你已经读过 [u7-l1（fs 模块）](u7-l1-fs-module.md)，并掌握以下概念：

- **双引擎双实现**：每个扩展功能都成对提供 `external/njs_*_module.c`（内置引擎，基于 `njs_value_t`/`njs_module_t`/外部原型）与 `external/qjs_*_module.c`（QuickJS，基于 `JSValue`/`qjs_module_t`/JS 类）两份实现。
- **声明表与 magic 复用**：一张静态声明表定义模块对外的属性/方法，多个 JS 方法可通过 `magic8`/`magic32` 共用同一个 C 函数（见 [u5-l4 外部对象](u5-l4-external-objects-and-native-functions.md)、[u7-l1](u7-l1-fs-module.md)）。
- **内存池**：内置引擎的所有运行时分配挂在 `vm->mem_pool` 上，VM 销毁时一次性回收（见 [u2-l3 内存池](u2-l3-memory-pool-and-hash.md)）。

此外需要一点领域常识：

- **百分号编码**：URL 里把不安全字符写成 `%HH`（两个十六进制位），空格在查询串里常被写成 `+`。
- **UTF-8**：一种变长 Unicode 编码，一个码点（codepoint）占 1～4 字节。
- **DEFLATE / INFLATE**：zlib 提供的压缩/解压算法；`MAX_WBITS = 15` 是默认滑动窗口大小。
- **XML DOM**：把 XML 文档解析成一棵由元素节点（element）、属性（attribute）、文本（text）组成的树。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [external/njs_query_string_module.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_query_string_module.c) | querystring 模块的内置引擎实现 |
| [external/qjs_query_string_module.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/qjs_query_string_module.c) | querystring 模块的 QuickJS 实现 |
| [external/njs_xml_module.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_xml_module.c) | xml 模块的内置引擎实现（XMLDoc/XMLNode/XMLAttr 对象模型、parse/c14n/serialize） |
| [external/qjs_xml_module.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/qjs_xml_module.c) | xml 模块的 QuickJS 实现（用 JS 类 + exotic methods） |
| [external/njs_zlib_module.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_zlib_module.c) | zlib 模块的内置引擎实现（deflate/inflate） |
| [external/qjs_zlib_module.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/qjs_zlib_module.c) | zlib 模块的 QuickJS 实现 |
| [src/qjs.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.h) | `QJS_CORE_CLASS_ID_XML_*` 类 id 枚举 |
| [auto/options](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/options) | `--no-libxml2`/`--no-zlib` 等构建开关 |
| [auto/libxml2](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/libxml2) | libxml2 特性检测脚本 |
| [auto/zlib](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/zlib) | zlib 特性检测脚本 |
| [auto/modules](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/modules) | 内置引擎扩展模块的条件化收录清单 |

---

## 4. 核心概念与源码讲解

### 4.1 querystring：URL 查询字符串的编解码

#### 4.1.1 概念说明

`querystring` 是从 Node.js 移植过来的一个工具模块，专门处理 URL 里 `?` 后面那段「键值对」字符串，例如 `baz=fuz&muz=tax`。它只有四个核心方法，并且成对提供别名：

| 方法 | 别名 | 作用 |
|---|---|---|
| `parse(str, sep, eq, options)` | `decode` | 把查询串解析成对象 |
| `stringify(obj, sep, eq, options)` | `encode` | 把对象序列化成查询串 |
| `escape(str)` | — | 对单个字符串做百分号编码 |
| `unescape(str)` | — | 对单个字符串做百分号解码 |

它解决的问题是：NGINX 处理 HTTP 请求时，`r.args` 就是这样一段原始字符串，业务代码经常需要把它变成结构化对象再使用。

#### 4.1.2 核心流程

`parse` 的执行过程可以用下面这段伪代码概括：

```
parse(query, sep="&", eq="=", options):
    obj = {}                          # 结果对象
    count = 0
    对 query 按 sep 切成一段段 pair:
        if count++ == maxKeys: break  # 默认 maxKeys=1000，0 表示无限
        在 pair 内按 eq 找到 key/value 分界
        key, value = unescape(key), unescape(value)
        if key 已存在于 obj:
            把旧值与新值合并成数组    # 重复键 → 数组
        else:
            obj[key] = value
    return obj
```

两个关键设计：

1. **可替换的编解码器**：`options.escape`/`options.unescape`（或 `encodeURIComponent`/`decodeURIComponent`）允许调用者传入自定义函数；不传则用模块自带的 `escape`/`unescape`。
2. **重复键折叠成数组**：`baz=fuz&baz=bar` 会得到 `{ baz: ['fuz', 'bar'] }`。

#### 4.1.3 源码精读

模块对外的形状由一张声明表定义。注意 `parse` 与 `decode` 指向同一个 C 函数，`stringify` 与 `encode` 也指向同一个 —— 这是 u7-l1 见过的「一个 C 函数服务多个 JS 名字」做法：

[external/njs_query_string_module.c:28-103](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_query_string_module.c#L28-L103) — 声明表 `njs_ext_query_string[]`，把 `parse`/`decode` 都绑到 `njs_query_string_parse`，`stringify`/`encode` 都绑到 `njs_query_string_stringify`。

[external/njs_query_string_module.c:106-110](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_query_string_module.c#L106-L110) — 注册结构 `njs_query_string_module`，`init = njs_query_string_init`，由 VM 启动期调用。

`njs_query_string_parse` 负责「参数解析 + 选项归一化」，最后把真正的切分工作交给 `njs_query_string_parser`。看默认值与选项处理：

[external/njs_query_string_module.c:379-463](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_query_string_module.c#L379-L463) — 第 380 行 `max_keys = 1000` 是默认上限；第 432-434 行 `max_keys == 0` 时改写为 `INT64_MAX`（即「不限制」）；第 437-447 行允许从 `options.decodeURIComponent` 取自定义解码器；若未提供则回退到模块自带 `unescape`。

真正的切分循环在 `njs_query_string_parser`：

[external/njs_query_string_module.c:475-525](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_query_string_module.c#L475-L525) — `njs_query_string_match` 在一段范围里查找 `sep`/`eq` 的位置；找到后由 `njs_query_string_append` 把键值写入对象。第 501 行 `if (part == key) goto next;` 跳过空段，这就是为什么 `&&baz=fuz` 仍能解析出 `{baz:'fuz'}`。

重复键折叠成数组的逻辑在 `njs_query_string_append`：

[external/njs_query_string_module.c:297-336](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_query_string_module.c#L297-L336) — 先读旧值（第 297-298 行）：若旧值已是数组就 `push` 新值（第 301-310 行）；否则新建一个含两个元素的数组替换之（第 312-332 行）。

百分号解码是本模块最有「算法味」的部分。`njs_query_string_decode` 用一张 256 项的查找表把 `%HH` 中的两个十六进制字符转成数值，再交给 UTF-8 解码器拼成码点；同时把 `+` 还原成空格：

[external/njs_query_string_module.c:123-209](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_query_string_module.c#L123-L209) — 第 134-153 行是 hex 查找表（`'0'..'9'`→0..9，`'A'..'F'`/`'a'..'f'`→10..15，其余为 -1）；第 164-166 行处理 `%HH`；第 169-170 行处理 `+`→空格；第 178-184 行把非法码点替换成 U+FFFD（替换字符），这与测试用例 `baz=%F6 → '�'`、`baz=%FG → '%FG'` 的行为一致。

> **解码条件的小陷阱**：第 164 行的判断是 `*p == '%' && end - p > 2 && hex[p[1]] >= 0 && hex[p[2]] >= 0`。这意味着末尾不完整的 `%F`、`%FG` 不会被当作转义，而是原样保留（对应测试 `{ value: 'baz=%F', expected: { baz:'%F' } }`）。

百分号**编码**（`escape`）用一个 256 位的位图 `escape[]` 决定每个字节是否需要转义：

[external/njs_query_string_module.c:534-550](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_query_string_module.c#L534-L550) — 8 个 `uint32_t` 共 256 位，每位对应一个字节值；位为 1 表示该字节需要 `%HH` 转义。需要转义的字符包括所有控制字符、空格、`=`、`&`、`%` 等。

#### QuickJS 侧的「原生标记」优化

QuickJS 版（`qjs_query_string_module.c`）逻辑同构，但有一个值得注意的优化。内置引擎可以用指针相等直接判断「这个解码器是不是自带的 `unescape`」（`njs_query_string_is_native_decoder`，第 212-227 行）；QuickJS 的 JS 函数没有稳定的 C 指针可比，于是它给自带的 `escape`/`unescape` 打了一个 `native: true` 属性作为标记：

[external/qjs_query_string_module.c:932-958](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/qjs_query_string_module.c#L932-L958) — `qjs_querystring_module_init` 在模块初始化时给 `escape`/`unescape` 两个方法挂上 `native` 布尔属性。

[external/qjs_query_string_module.c:116-125](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/qjs_query_string_module.c#L116-L125) — `parse` 里读取 `decode.native`，若为真就把 `decode` 置为 `JS_NULL`，从而走 `qjs_query_string_decode` 这条不经 `JS_Call` 的快路径。

这是「同一份业务逻辑，因引擎对象模型不同而用不同手段实现同一优化」的典型例子。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `querystring.parse` 的默认行为与重复键折叠。

**操作步骤**（先按 [u1-l3](u1-l3-build-and-run-cli.md) 构建出 `build/njs`）：

1. 写一个最小脚本 `qs_demo.js`（示例代码）：

```javascript
import qs from 'querystring';

// 1) 基础解析
console.log(qs.parse('a=1&b=2'));

// 2) 重复键折叠成数组
console.log(qs.parse('baz=fuz&baz=bar'));

// 3) + 还原为空格，%HH 还原
console.log(qs.parse('ba+z=f%32uz'));

// 4) maxKeys 限制
console.log(qs.parse('a=1&b=2', null, null, { maxKeys: 1 }));

// 5) 反向序列化
console.log(qs.stringify({ a: '1', b: ['x', 'y'] }));
```

2. 分别用两个引擎运行：

```bash
./build/njs -n njs      qs_demo.js   # 内置引擎（若已链接 QuickJS）
./build/njs -n QuickJS  qs_demo.js   # QuickJS 引擎
```

**需要观察的现象**：第 1 项得到 `{ a:'1', b:'2' }`；第 2 项得到 `{ baz:['fuz','bar'] }`；第 3 项得到 `{ 'ba z':'f2uz' }`；第 4 项只得到 `{ a:'1' }`；第 5 项得到 `a=1&b=x&b=y`。

**预期结果**：两个引擎输出一致（这是双实现的目标——API 形状一致）。

> 若未链接 QuickJS，`-n QuickJS` 会报错；可只跑内置引擎部分。具体报错信息「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`qs.parse('===fu=z&baz=bar')` 的结果是什么？为什么？

**参考答案**：`{ baz:'bar', '':'==fu=z' }`。`eq` 默认是 `=`，匹配在 `njs_query_string_match` 里找**第一个** `=`，因此 key 是空串、value 是 `==fu=z`；第二段正常得到 `baz:'bar'`。

**练习 2**：为什么 `njs_query_string_decode` 里 `hex[p[1]] >= 0 && hex[p[2]] >= 0` 这个判断对 `%FG` 会失败？

**参考答案**：`'G'` 在 hex 表里是 -1，`>= 0` 不成立，因此 `%FG` 不被当作转义序列，原样保留。

---

### 4.2 xml：基于 libxml2 的 XML 对象模型

#### 4.2.1 概念说明

`xml` 模块提供 XML 解析、遍历、修改与规范化（canonicalization）能力，典型用途是处理 SAML 断言、SOAP 报文等 XML 格式的安全协议数据。它**不是**用纯 C 重新实现一个 XML 解析器，而是包装了成熟的开源库 **libxml2**：解析、树操作、c14n 这些重活都交给 libxml2，njs 只负责把 libxml2 的 C 数据结构（`xmlDoc`/`xmlNode`）映射成 JavaScript 可操作的对象。

模块对外暴露三类对象：

| 对象 | 对应 libxml2 结构 | 说明 |
|---|---|---|
| `XMLDoc` | `xmlDoc` | 整个文档，`xml.parse()` 的返回值 |
| `XMLNode` | `xmlNode` | 一个元素节点 |
| `XMLAttr` | `xmlAttr` | 一个属性 |

并设计了一套独特的「属性访问语法」：在普通属性名前加 `$` 前缀来表达不同语义（见 4.2.3）。

#### 4.2.2 核心流程

`xml.parse(xmlString)` 的流程：

```
parse(data):
    tree = 分配 njs_xml_doc_t { ctx, doc }
    tree.ctx = xmlNewParserCtxt()              # libxml2 解析上下文
    tree.doc = xmlCtxtReadMemory(ctx, data)    # 真正解析，得到 xmlDoc
    注册 cleanup：VM 销毁时回调 njs_xml_doc_cleanup 释放 xmlDoc
    return 包成 XMLDoc 外部对象(tree)
```

得到 `XMLDoc` 后，对它的属性访问会被一个特殊的 `prop_handler` 拦截，根据**属性名的前缀**决定行为：

```
访问 doc.note           → 找名为 "note" 的根/子元素，返回 XMLNode
访问 node.$tag$foo      → 找第一个名为 "foo" 的子元素
访问 node.$tags$foo     → 找所有名为 "foo" 的子元素，返回数组
访问 node.$attr$foo     → 取名为 "foo" 的属性，返回 XMLAttr/字符串
访问 node.$attrs        → 属性集合
访问 node.$text         → 节点的文本内容
访问 node.$name/$ns/$parent → 节点名/命名空间/父节点
```

#### 4.2.3 源码精读

模块顶层的四个方法 `parse`/`c14n`/`exclusiveC13n`/`serialize`/`serializeToString` 在声明表里，其中后四个序列化方法共用同一个 C 函数 `njs_xml_ext_canonicalization`，靠 `magic8` 区分（`exclusiveC13n` 的 `magic8=1`，`serializeToString` 的 `magic8=2`）：

[external/njs_xml_module.c:128-195](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_xml_module.c#L128-L195) — 顶层声明表 `njs_ext_xml[]`。

`parse` 的实现很薄，核心就是调用 libxml2 并注册清理：

[external/njs_xml_module.c:424-468](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_xml_module.c#L424-L468) — 第 443 行 `xmlNewParserCtxt()`、第 449-451 行 `xmlCtxtReadMemory(...)` 真正解析；第 457-464 行用 `njs_mp_cleanup_add` 把 `njs_xml_doc_cleanup` 挂到内存池的清理链上；第 466-467 行用 `njs_vm_external_create` 把 C 指针包成 JS 对象。这正是 [u2-l3 内存池](u2-l3-memory-pool-and-hash.md) 讲过的 cleanup 链机制——libxml2 自己分配的 `xmlDoc` 不是池内内存，必须靠 cleanup 回调在 VM 销毁时 `xmlFreeDoc` 回收。

`XMLDoc`/`XMLNode` 的属性访问不是固定属性，而是动态的，所以它们用了一个特殊的 `NJS_EXTERN_SELF` 条目，挂上 `prop_handler` 拦截**所有**属性读写：

[external/njs_xml_module.c:198-227](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_xml_module.c#L198-L227) — `njs_ext_xml_doc[]`，第 208-215 行的 `NJS_EXTERN_SELF` 把 `njs_xml_doc_ext_root` 设为全对象属性处理器。

[external/njs_xml_module.c:230-249](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_xml_module.c#L230-L249) — `njs_ext_xml_node[]` 同样用 `NJS_EXTERN_SELF` + `njs_xml_node_ext_prop_handler`。

节点属性处理器解析 `$` 前缀的语义，是理解这套 API 的钥匙：

[external/njs_xml_module.c:665-719](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_xml_module.c#L665-L719) — 第 673-678 行的注释精确描述了四种前缀；第 692 行起依次判断 `$attr$`/`$tag$`/`$tags$` 并分派到对应 handler。不带 `$` 的普通名字（如 `note`）等价于 `$tag$note`。

模块初始化时把三张声明表编译成三个整数句柄 `proto_id`，之后所有 `njs_vm_external_create`/`njs_vm_external` 都用这些句柄：

[external/njs_xml_module.c:2038-2052](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_xml_module.c#L2038-L2052) — 注册三个原型 `njs_xml_doc_proto_id`/`njs_xml_node_proto_id`/`njs_xml_attr_proto_id`。

#### QuickJS 侧：用 JS 类 + exotic methods 替代外部原型

内置引擎用「外部原型 + prop_handler」表达动态对象（见 [u5-l4](u5-l4-external-objects-and-native-functions.md)）；QuickJS 没有这套机制，转而注册三个 **JS 类**，并通过 `JSClassExoticMethods` 提供自定义的属性 get/set/delete 钩子。三个类的 id 集中定义在枚举里：

[src/qjs.h:37-39](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.h#L37-L39) — `QJS_CORE_CLASS_ID_XML_DOC`/`XML_NODE`/`XML_ATTR`（如 [u6-l1](u6-l1-quickjs-wrapper.md) 所述，这些 id 从 64 起避开 QuickJS 内建类）。

[external/qjs_xml_module.c:153-184](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/qjs_xml_module.c#L153-L184) — 三个 `JSClassDef`，各自带 `finalizer`（负责释放资源）和 `exotic` 方法表（负责属性访问）。

QuickJS 版的 `parse` 用 `JS_NewObjectClass(cx, QJS_CORE_CLASS_ID_XML_DOC)` 创建对象、`JS_SetOpaque(ret, tree)` 把 libxml2 指针塞进对象 opaque 槽：

[external/qjs_xml_module.c:195-240](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/qjs_xml_module.c#L195-L240) — 第 213 行 `xmlNewParserCtxt()`、第 221 行 `xmlCtxtReadMemory(...)`、第 231 行 `JS_NewObjectClass`、第 237 行 `JS_SetOpaque`。

> **两引擎资源回收方式不同**：内置引擎靠内存池 cleanup 链（VM 销毁时统一回收）；QuickJS 靠类的 `finalizer` + `ref_count` 引用计数（GC 回收对象时回调 `qjs_xml_doc_finalizer`）。这是「同一份功能、因引擎生命周期模型不同而用不同回收策略」的又一例。QuickJS 的 `qjs_xml_doc_t` 因此多了一个 `ref_count` 字段（[external/qjs_xml_module.c:15-20](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/qjs_xml_module.c#L15-L20)），因为一个 `xmlDoc` 会被多个 `XMLNode`/`XMLAttr` 对象共享，需要引用计数决定何时真正 `xmlFreeDoc`。

#### 4.2.4 代码实践

**实践目标**：用 `xml.parse` 解析一段 XML，验证 `$tag`/`$attrs`/`$text` 访问语法。

**操作步骤**：

1. 先确认构建启用了 libxml2（默认启用，见 4.4）。写脚本 `xml_demo.js`（示例代码，参考 `test/xml/xml.t.mjs`）：

```javascript
import xml from 'xml';

let doc = xml.parse('<note><to a="foo" b="bar">Tove</to><from>Jani</from></note>');

console.log(doc.note.$name);            // 'note'
console.log(doc.note.to.$text);          // 'Tove'
console.log(doc.note.to.$attrs.a);       // 'foo'
console.log(doc.note.to.$attr$b);        // 'bar'
console.log(doc.note.$tag$from.$text);   // 'Jani'
console.log(doc.note.$tags.length);      // 2
console.log(doc.note.$tags[1].$text);    // 'Jani'
```

2. 运行：

```bash
./build/njs xml_demo.js
```

**需要观察的现象**：每个属性访问都按 4.2.2 的语义表给出对应结果；`$tags` 是数组。

**预期结果**：输出依次为 `note / Tove / foo / bar / Jani / 2 / Jani`。

> 若构建时用 `--no-libxml2` 或系统未装 libxml2，`import xml from 'xml'` 会失败。具体报错信息「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：访问 `node.foo` 与 `node.$tag$foo` 有何关系？源码依据在哪？

**参考答案**：二者等价，都表示「第一个名为 `foo` 的子元素」。依据在 `njs_xml_node_ext_prop_handler` 的注释（第 677 行 `foo - the same as $tag$foo`）：当属性名不带 `$` 前缀时，处理器会把它当成 `$tag$<名字>` 处理。

**练习 2**：为什么 QuickJS 版的 `qjs_xml_doc_t` 需要 `ref_count`，而内置引擎版 `njs_xml_doc_t` 不需要？

**参考答案**：内置引擎所有 `XMLNode`/`XMLAttr` 的生命周期都由 VM 内存池统一管理，VM 销毁时一次性 cleanup，无需引用计数；QuickJS 的每个 JS 对象由 GC 独立回收，多个 node/attr 对象共享同一个 `xmlDoc`，必须用引用计数判断「最后一个引用消失时」才能安全 `xmlFreeDoc`。

---

### 4.3 zlib：DEFLATE 压缩与解压

#### 4.3.1 概念说明

`zlib` 模块（注意：是 Node 风格的同步 API，名字就叫 `zlib`）提供基于系统 **zlib 库** 的压缩/解压能力。它只暴露四个同步方法与一组常量：

| 方法 | 作用 |
|---|---|
| `deflateSync(data, options)` | zlib 格式压缩（带 zlib 头） |
| `deflateRawSync(data, options)` | raw DEFLATE 压缩（无头） |
| `inflateSync(data, options)` | zlib 格式解压 |
| `inflateRawSync(data, options)` | raw DEFLATE 解压 |

`constants` 子对象暴露 `Z_NO_COMPRESSION`/`Z_BEST_SPEED`/`Z_BEST_COMPRESSION` 等压缩级别与策略常量。典型用途是在 NGINX 里对响应体做压缩预处理，或与外部系统交换压缩数据。

#### 4.3.2 核心流程

`deflateSync` 的流程（`inflateSync` 对称）：

```
deflate(data, options):
    读取并校验 options: level/windowBits/memLevel/strategy/chunkSize/dictionary
    stream.zalloc = njs_zlib_alloc   # 让 zlib 用 njs 内存池分配
    stream.zfree  = njs_zlib_free    # free 是空操作（池统一回收）
    deflateInit2(stream, level, ...)
    若有 dictionary: deflateSetDictionary(stream, dict)
    do:
        stream.next_out = 预留 chunk_size 输出缓冲
        deflate(stream, Z_FINISH)
        记录本块实际产出字节数
    while 输出缓冲被填满（还有数据没吐完）
    deflateEnd(stream)
    把所有输出块拼接成一段连续 buffer 返回
```

关键设计点：

1. **`deflateSync`/`deflateRawSync` 共用一个 C 函数**，差别只在 `window_bits` 的正负号。
2. **zlib 的内存分配被重定向到 njs 内存池**：`zalloc` 走 `njs_mp_alloc`，`zfree` 是空操作。这样压缩产生的所有临时内存都归池管理，VM 销毁时零散地自动回收，不会泄漏。

#### 4.3.3 源码精读

声明表里，`deflateRawSync`/`deflateSync` 共用 `njs_zlib_ext_deflate`，靠 `magic8` 区分（raw=1，非 raw=0）；`inflateRawSync`/`inflateSync` 同理：

[external/njs_zlib_module.c:120-162](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_zlib_module.c#L120-L162) — `deflateRawSync` 的 `magic8=1`（第 127 行），`deflateSync` 的 `magic8=0`（第 138 行）。`magic8` 会作为 `raw` 参数传进 C 函数。

`magic8` 是如何变成 `raw` 参数的：函数签名第 4 个参数是 `njs_index_t raw`，引擎在调用原生方法时把 `magic8` 填进来。`raw` 直接决定 `window_bits` 的符号——这是 raw 与标准 zlib 格式的唯一区别：

[external/njs_zlib_module.c:185-217](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_zlib_module.c#L185-L217) — 第 217 行 `window_bits = raw ? -MAX_WBITS : MAX_WBITS;`。zlib 约定：`window_bits` 为正在值（9..15）产生带 zlib 头的流；为负值产生 raw 流。

选项校验非常严格，超范围就抛 `RangeError`/`TypeError`，这些边界正是测试用例覆盖的重点：

[external/njs_zlib_module.c:219-300](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_zlib_module.c#L219-L300) — 第 226-229 行 `chunkSize < 64` 报错；第 236-242 行 `level` 范围是 `Z_DEFAULT_COMPRESSION(-1)..Z_BEST_COMPRESSION(9)`；第 249-262 行 `windowBits` raw 须在 `-15..-9`、非 raw 须在 `9..15`；第 279-290 行 `strategy` 必须是已知枚举值之一。

内存分配重定向与压缩主循环：

[external/njs_zlib_module.c:302-346](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_zlib_module.c#L302-L346) — 第 305-307 行把 `stream.zalloc`/`zfree`/`opaque` 指向 njs 的包装函数与内存池；第 309-310 行 `deflateInit2`；第 326-344 行循环调用 `deflate(&stream, Z_FINISH)` 直到输出缓冲不再被填满。

[external/njs_zlib_module.c:561-572](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_zlib_module.c#L561-L572) — `njs_zlib_alloc` 直接转发给 `njs_mp_alloc`；`njs_zlib_free` 是空操作。这是「池式内存模型对外部库的适配」的最佳范例：zlib 内部分配的内存因此全部纳入 njs 内存池，无需 zlib 自己管理释放。

解压 `njs_zlib_ext_inflate` 结构对称，区别在于循环终止条件是 `rc != Z_STREAM_END`，且需处理 `Z_NEED_DICT`（压缩时用了字典、解压时没提供）这一特例：

[external/njs_zlib_module.c:444-489](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_zlib_module.c#L444-L489) — 第 451 行 `inflateInit2`；第 467 行 `while (rc != Z_STREAM_END)`；第 483-486 行 `Z_NEED_DICT` 抛 `TypeError: ... dictionary is required`（对应测试用例的异常）。

QuickJS 版（`qjs_zlib_module.c`）用 `JS_CFUNC_MAGIC_DEF` 的第 5 个参数（magic）传 raw 标志，常量用 `JS_PROP_INT32_DEF` 声明，业务逻辑与内置引擎版几乎逐行对应：

[external/qjs_zlib_module.c:49-64](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/qjs_zlib_module.c#L49-L64) — 导出表与注册结构。

#### 4.3.4 代码实践

**实践目标**：压缩一段数据再解压，验证往返一致，并观察常量与 `magic8` 分流。

**操作步骤**（先确认构建启用了 zlib，默认启用）：

1. 写脚本 `zlib_demo.js`（示例代码，参考 `test/zlib.t.mjs`）：

```javascript
import zlib from 'zlib';

// 1) raw 压缩 → base64，便于肉眼对比
let raw = zlib.deflateRawSync('WAKA');
console.log(raw.toString('base64'));   // 期望 C3f0dgQA

// 2) 带 zlib 头的压缩
let withHeader = zlib.deflateSync('WAKA');
console.log(withHeader.toString('base64'));  // 期望 eJwLd/R2BAAC+gEl

// 3) 往返：用同样的 raw 解压器还原
console.log(zlib.inflateRawSync(raw).toString());  // 期望 WAKA

// 4) 用常量指定不压缩
let stored = zlib.deflateRawSync('WAKA',
              { level: zlib.constants.Z_NO_COMPRESSION });
console.log(stored.toString('base64'));  // 期望 AQQA+/9XQUtB
```

2. 运行：

```bash
./build/njs zlib_demo.js
```

**需要观察的现象**：第 1、2 项的 base64 输出与 `test/zlib.t.mjs` 里的期望值一致；第 3 项还原回 `WAKA`；第 4 项因 `Z_NO_COMPRESSION` 产生「存储式」（stored）压缩，体积反而略大于原文。

**预期结果**：依次输出 `C3f0dgQA / eJwLd/R2BAAC+gEl / WAKA / AQQA+/9XQUtB`（base64 串取决于 zlib 版本，若略有不同属正常，但 `WAKA` 往返必须严格一致）。

> 若构建时用 `--no-zlib` 或系统缺 zlib，`import zlib from 'zlib'` 会失败。具体报错信息「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `deflateRawSync` 与 `deflateSync` 可以共用同一个 C 函数？它们的差别在源码里如何体现？

**参考答案**：二者唯一差别是输出是否带 zlib 头，zlib 库用 `window_bits` 的正负号区分。共用函数 `njs_zlib_ext_deflate` 通过 `magic8` 收到 `raw` 标志，在第 217 行 `window_bits = raw ? -MAX_WBITS : MAX_WBITS` 决定符号，因此一份代码服务两个方法。

**练习 2**：`njs_zlib_free` 为什么是空操作？这样安全吗？

**参考答案**：因为 zlib 的 `zalloc` 已被重定向到 `njs_mp_alloc`（第 564 行），所有内存都来自 njs 内存池；而内存池的生命周期与 VM 绑定，VM 销毁时整体回收。把 `zfree` 设为空操作不会泄漏，反而避免了「zlib 与池双重释放」。这是池式内存模型对外部库的标准适配方式。

---

### 4.4 可选依赖：构建期特性检测

#### 4.4.1 概念说明

`querystring` 是纯 C 实现，无外部依赖；但 **`xml` 依赖 libxml2、`zlib` 依赖 zlib 库**。这两个库是「可选依赖」：用户可能没装，或明确想裁掉。njs 用一套自研的 shell 构建系统在 `./configure` 阶段做特性检测——能找到就编入对应模块，找不到就跳过。这意味着同一个 `build/njs` 在不同机器上可用的模块可能不同。

#### 4.4.2 核心流程

```
./configure
  → auto/options: NJS_LIBXML2=YES / NJS_ZLIB=YES (默认开启)
                   --no-libxml2 / --no-zlib 可关掉
  → auto/libxml2: 探测系统 libxml2 (pkg-config → 标准路径 → 各 OS 端口路径)
                   找到 → NJS_HAVE_LIBXML2=YES，把 -lxml2 加进链接库
  → auto/zlib:    同理探测 zlib → NJS_HAVE_ZLIB=YES
  → auto/modules: 仅当 NJS_LIBXML2=YES 且 NJS_HAVE_LIBXML2=YES 才收录 njs_xml_module
                   仅当 NJS_ZLIB=YES 且 NJS_HAVE_ZLIB=YES 才收录 njs_zlib_module
                   query_string 模块无条件收录
```

注意是**两个条件都满足**才编入：用户用 `--no-libxml2` 主动关闭（第一个条件不满足），或系统没装（第二个条件不满足），效果都是模块不编入。

#### 4.4.3 源码精读

默认开关在 `auto/options`，并用 `--no-*` 选项允许关闭：

[auto/options:21-23](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/options#L21-L23) — `NJS_OPENSSL`、`NJS_LIBXML2`、`NJS_ZLIB` 默认都是 `YES`。

[auto/options:57-59](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/options#L57-L59) — `--no-openssl`/`--no-libxml2`/`--no-zlib` 把对应开关置 `NO`。

libxml2 探测脚本先试 `pkg-config`，失败再依次试标准路径、FreeBSD/NetBSD/MacPorts 等端口路径（这种「多策略回退」模式与 [u1-l3](u1-l3-build-and-run-cli.md) 讲过的 QuickJS 探测一致）：

[auto/libxml2:8-95](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/libxml2#L8-L95) — 第 8 行 `if [ $NJS_LIBXML2 = YES ]` 是总开关；第 24-33 行先试 pkg-config；第 35-75 行依次回退到各系统路径；第 77-94 行探测成功后置 `NJS_HAVE_LIBXML2=YES` 并把库追加进 `NJS_LIB_AUX_LIBS`。

zlib 探测结构相同，更简单（库通常就在标准位置）：

[auto/zlib:8-61](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/zlib#L8-L61) — 第 24-33 行 pkg-config；第 35-41 行标准 `-lz`；第 43-59 行成功后置 `NJS_HAVE_ZLIB=YES`。

最终，`auto/modules` 用「双条件与」决定是否收录 xml/zlib，而 `query_string` 与 `fs`、`buffer` 一样无条件收录：

[auto/modules:24-50](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/modules#L24-L50) — 第 24-30 行 `if [ $NJS_LIBXML2 = YES -a $NJS_HAVE_LIBXML2 = YES ]` 收录 xml；第 32-38 行同理收录 zlib；第 46-50 行无条件收录 query_string。QuickJS 侧的 `auto/qjs_modules` 用同样的条件收录 `qjs_xml_module`/`qjs_zlib_module`。

#### 4.4.4 代码实践

**实践目标**：亲手验证「可选依赖裁剪」对可用模块的影响。

**操作步骤**：

1. 正常构建（默认启用 libxml2 与 zlib），确认 xml/zlib 可用：

```bash
./configure && make njs
./build/njs -c "import zlib from 'zlib'; console.log(typeof zlib.deflateRawSync)"
./build/njs -c "import xml from 'xml'; console.log(typeof xml.parse)"
```

2. 重新配置，裁掉 zlib 与 libxml2，再构建：

```bash
make clean
./configure --no-zlib --no-libxml2 && make njs
./build/njs -c "import zlib from 'zlib'; console.log(typeof zlib.deflateRawSync)"
./build/njs -c "import xml from 'xml'; console.log(typeof xml.parse)"
./build/njs -c "import qs from 'querystring'; console.log(typeof qs.parse)"
```

**需要观察的现象**：第 1 步两条命令分别输出 `function`、`function`；第 2 步前两条命令应报模块找不到的错（具体错误形态「待本地验证」），第三条 `querystring` 仍输出 `function`（因为它无外部依赖，永远编入）。

**预期结果**：`--no-zlib`/`--no-libxml2` 精确移除对应模块，而 `querystring` 不受影响。这印证了「可选依赖只在构建期决定、与引擎选择无关」。

#### 4.4.5 小练习与答案

**练习 1**：如果一台机器没装 libxml2，但用户既没加 `--no-libxml2` 也没做任何处理，构建会发生什么？xml 模块会编入吗？

**参考答案**：不会编入。`auto/libxml2` 所有探测路径都失败，`NJS_HAVE_LIBXML2` 保持 `NO`；随后 `auto/modules` 第 24 行的双条件与 `$NJS_LIBXML2 = YES -a $NJS_HAVE_LIBXML2 = YES` 不成立，xml 模块被跳过。构建仍能成功，只是缺 xml。

**练习 2**：为什么 `query_string` 模块在 `auto/modules` 里没有 `if` 守卫，而 xml/zlib 有？

**参考答案**：`query_string` 是纯 C 实现，无任何外部库依赖，任何环境都能编译；xml/zlib 分别依赖 libxml2/zlib，必须先确认依赖存在（`NJS_HAVE_*`）且未被用户关闭（`NJS_*`）才能编入，故需要条件守卫。

---

## 5. 综合实践

把三个模块串起来完成一个小任务：**解析一段带查询参数的 XML 请求描述，压缩后输出**。

任务背景：模拟收到一段形如 `data=<url编码的XML>` 的查询串，要求把其中的 XML 取出来、解析、读取某节点文本，再把结果用 zlib 压缩成 base64 输出。

参考实现（示例代码）：

```javascript
import qs from 'querystring';
import xml from 'xml';
import zlib from 'zlib';

// 模拟收到的查询串：data 后面跟一段 URL 编码的 XML
let args = 'src=cli&data=' + qs.escape('<note><to>Tove</to><from>Jani</from></note>');

// 1) 用 querystring 解析查询串，取 data 字段
let params = qs.parse(args);
console.log('src =', params.src);

// 2) 解析 XML，读取 note.from 的文本
let doc = xml.parse(params.data);
let who = doc.note.from.$text;
console.log('from =', who);

// 3) 把结果压缩成 base64
let packed = zlib.deflateRawSync(who);
console.log('packed =', packed.toString('base64'));
```

跟踪要点（源码阅读型实践）：

1. 在 `njs_query_string_parse` 里确认 `data` 字段的值会被 `unescape` 还原成原始 XML 字符串。
2. 在 `njs_xml_ext_parse` 里确认 `params.data` 这段字符串经 `xmlCtxtReadMemory` 解析成 `XMLDoc`。
3. 在 `njs_xml_node_ext_prop_handler` 里确认 `doc.note.from` 经过两次「`$tag$` 语义」解析得到 `note` 下的 `from` 节点，再 `.$text` 取文本。
4. 在 `njs_zlib_ext_deflate` 里确认 `who` 经 DEFLATE 压缩，且输出 buffer 由内存池持有。

预期输出（base64 串取决于 zlib 版本，可能略有不同）：

```
src = cli
from = Jani
packed = <某 base64 串>
```

> 若环境未启用 libxml2/zlib，此脚本会失败；可先用 4.4 的方法确认依赖，或退化为只用 `querystring` 部分完成子任务。

---

## 6. 本讲小结

- `querystring` 是无外部依赖的纯 C 模块，提供 `parse`/`stringify`/`escape`/`unescape`（及 `decode`/`encode` 别名）；核心是 `njs_query_string_parser` 的 sep/eq 切分、重复键折叠成数组，以及基于 hex 表与 `escape` 位图的百分号编解码。
- 双引擎在 querystring 上逻辑同构，但 QuickJS 用 `native` 属性标记自带编解码器来实现「快路径检测」（内置引擎用指针相等），体现了同一优化在两种对象模型下的不同实现手段。
- `xml` 模块包装 libxml2，对外暴露 `XMLDoc`/`XMLNode`/`XMLAttr` 三类对象；内置引擎用「外部原型 + `NJS_EXTERN_SELF` prop_handler」，QuickJS 用「JS 类 + exotic methods」，并通过 `$tag`/`$tags`/`$attr`/`$attrs`/`$text`/`$name`/`$ns`/`$parent` 前缀语法把属性访问映射到 libxml2 树操作。
- 两引擎对 xml 的资源回收方式不同：内置引擎靠内存池 cleanup 链，QuickJS 靠类 finalizer + `ref_count` 引用计数。
- `zlib` 模块包装系统 zlib 库，`deflateSync`/`deflateRawSync`（及 inflate 对）靠 `magic8`/`raw` 标志共用一个 C 函数；它把 zlib 的 `zalloc`/`zfree` 重定向到 njs 内存池，使压缩临时内存随 VM 自动回收。
- libxml2 与 zlib 都是**可选依赖**：`auto/options` 默认开启但可用 `--no-libxml2`/`--no-zlib` 关闭，`auto/libxml2`/`auto/zlib` 做 pkg-config + 多路径回退探测，`auto/modules` 用「用户开关 ∧ 探测成功」双条件决定是否编入；`querystring` 因无依赖而无条件编入。

---

## 7. 下一步学习建议

- 进入 [u8 NGINX 集成基础](u8-l1-ngx-js-shared-layer.md)，看这三个扩展模块如何与 `ngx_http_js_module` 配合在真实请求处理中被使用（例如在 `js_content` handler 里调 `xml.parse(r.variables.body)`）。
- 阅读 `test/xml/xml.t.mjs`、`test/zlib.t.mjs`、`test/querystring.t.mjs` 三个测试文件的完整用例，它们是最权威的「行为契约」，覆盖了大量边界条件（非法编码、字典缺失、windowBits 越界等）。
- 若想深入 xml 的规范化（c14n/exclusiveC13n）用途，可阅读 `test/xml/saml_verify.t.mjs`，它展示了 xml 模块与 [u7-l2 WebCrypto](u7-l2-crypto-and-webcrypto.md) 配合做 SAML 签名验证的完整链路。
- 对照本讲的「可选依赖探测」机制，回看 [u1-l2 构建系统](u1-l2-directory-and-build-system.md) 与 [u10-l3 进阶构建](u10-l3-build-advanced-and-sanitizers.md)，理解 `--no-*` 选项、`NJS_HAVE_*` 宏与 `NJS_LIB_AUX_LIBS` 如何贯穿整个构建系统。
