# u4-l8 序列化与结构化比较

## 1. 本讲目标

学完本讲,你应该能够:

1. 说清 PyPTO IR 的 `.pto`/MessagePack 序列化格式:节点如何带 ID 写出、同一对象如何只写一次、引用如何用 `{"ref": id}` 还原指针共享。
2. 独立完成一次「构造 IR → 序列化 → 反序列化 → `assert_structural_equal` 验证等价」的往返实验,并知道往返比较为什么要开 `enable_auto_mapping=True`。
3. 掌握 `structural_equal` / `assert_structural_equal` 在 Pass 测试(before/after 范式)中的标准用法,以及 IgnoreField / UsualField / DefField 三类字段在比较中的不同待遇。
4. 理解表达式级比较器 `AreExprsEqual` 的比较粒度——ConstInt 按值、二元/一元/Call 按结构、其余按指针——以及本版本新加入的一元表达式(Cast)分支;并能解释它与 `HashExprForAreExprsEqual` 的粒度为什么必须一比一同步。

本讲是 u4-l7(Builder 与打印器)的姊妹篇:u4-l7 讲的是**文本**往返(打印 → 再解析),本讲讲**二进制**往返(序列化 → 反序列化),两者共用同一个裁判——结构化相等比较。

## 2. 前置知识

- **为什么需要序列化**:Pass 流水线跑完之后,编译器手里是一棵 C++ 对象构成的 IR 图。要把它存进磁盘缓存、跨进程传给运行时、或写进 `.pto` 产物,必须先把对象图压平成字节流;读回来时再重建出等价的图。
- **MessagePack**:一种二进制 JSON。比 JSON 更紧凑、解析更快,天然适合「机器写给机器读」。PyPTO 的序列化产物就是 MessagePack 字节(Python 侧拿到的 `ir.serialize(...)` 返回值是 `bytes`)。
- **指针共享(pointer sharing)**:IR 是一张图而不是一棵树——同一个 `Var` 对象可能被几十处引用。序列化时如果每次引用都完整拷贝一份,反序列化后就会得到几十个「长得一样但不是同一个」的对象,指针身份(例如 MemRef 的分配身份)就丢了。PyPTO 的做法是每个对象只写一次,后续引用写 `{"ref": id}`。
- **结构化相等(structural equality)**:C++ 的 `==` 对智能指针比较的是地址;两个独立构造、内容相同的 IR 节点是「不相等」的。测试与缓存需要的是「忽略指针、忽略源位置(Span)、按字段递归比较」的相等,这就是 `structural_equal`。
- **哈希一致性契约**:若 `structural_equal(a, b)` 为真,则 `structural_hash(a) == structural_hash(b)` 必须成立,否则以哈希为键的容器(`dict` / `set` / CSE 缓存)会出错。这个契约同样约束着表达式级的 `AreExprsEqual` 与 `HashExprForAreExprsEqual` 这对搭档。

## 3. 本讲源码地图

| 文件 | 作用 |
| ---- | ---- |
| [src/ir/serialization/serializer.cpp](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/serialization/serializer.cpp) | 序列化器:IR 图 → MessagePack 字节,维护 `ptr_to_id_` 指针去重表 |
| [src/ir/serialization/deserializer.cpp](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/serialization/deserializer.cpp) | 反序列化器:字节 → IR 图,维护 `id_to_ptr_` 引用还原表,按类型注册表构造节点 |
| [src/ir/transforms/structural_equal.cpp](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/structural_equal.cpp) | 结构化相等比较器(反射字段遍历 + 变量自动映射 + 断言模式的报错路径) |
| [src/ir/expr.cpp](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/expr.cpp) | 表达式级比较器 `AreExprsEqual`(本版本新增一元表达式分支) |
| [src/ir/type.cpp](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/type.cpp) | `TileView` 的 `==`/`Hash`,以及与之配套的 `HashExprForAreExprsEqual` |
| [src/ir/transforms/structural_hash.cpp](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/structural_hash.cpp) | 节点级 `structural_hash` 的实现(变量按 `UniqueId` 参与哈希) |
| [tests/ut/ir/transforms/test_serialization.py](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/ir/transforms/test_serialization.py) | 序列化往返测试全集(本讲实践的样板) |
| [tests/ut/ir/core/test_tile_view_equality.py](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/ir/core/test_tile_view_equality.py) | TileView 相等/哈希一致性回归测试,含新增的 `TestUnaryExprEquality` |
| [docs/en/dev/ir/04-serialization.md](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/ir/04-serialization.md) | 序列化官方文档(格式速查) |
| [docs/en/dev/ir/03-structural_comparison.md](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/ir/03-structural_comparison.md) | 结构化比较官方文档(字段三类、自动映射) |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块:序列化器、反序列化器、节点级结构化相等、表达式级比较粒度与哈希同步。

### 4.1 序列化器:把 IR 图写成 MessagePack 字节流

#### 4.1.1 概念说明

序列化解决的问题是:**把 C++ 对象图无损地压平成字节,并且读回来后不仅内容一样,连「哪些引用指向同一个对象」也要一样**。

PyPTO 的格式很朴素,每个节点写成:

```javascript
// 首次出现的完整节点
{"id": 123, "type": "Add", "fields": {"left": {...}, "right": {...}, "dtype": 19, "span": {...}}}

// 之后再次引用同一对象
{"ref": 123}
```

之所以要保留指针身份,是因为 IR 里有一类正确性依赖「同一个对象」的语义——最典型的是 MemRef 的分配身份(4.2.3 会看到):两个 MemRef 是否属于同一块分配,看的是 `base_` 的**指针**相等,不是名字相等。

#### 4.1.2 核心流程

```text
IR 节点
  │
  ▼
IRSerializer::Impl::Serialize(node)
  ├─ ptr_to_id_.clear(); next_id_ = 0     ← 每次序列化重置编号
  ├─ SerializeNode(node, zone)            ← 深度优先遍历
  │    ├─ 查 ptr_to_id_:见过 → 写 {"ref": id} 返回
  │    ├─ 没见过 → 分配新 id,登记 ptr_to_id_
  │    ├─ 写 {"id", "type": TypeName(), "fields"}
  │    └─ SerializeFields → 按具体 Kind 分派 → 字段访问器逐字段序列化
  ▼
MessagePack 字节流(std::vector<uint8_t> → Python bytes)
```

复杂度 \(O(N)\)(N 为去重后的节点数):每个对象只完整写一次,引用是常数开销。

#### 4.1.3 源码精读

序列化入口,清空去重表后从根节点深度优先写出([src/ir/serialization/serializer.cpp:153-165](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/serialization/serializer.cpp#L153-L165)):

```cpp
std::vector<uint8_t> Serialize(const IRNodePtr& node) {
  ptr_to_id_.clear();
  next_id_ = 0;
  ...
  auto obj = SerializeNode(node, zone);
  packer.pack(obj);
  return std::vector<uint8_t>(buffer.data(), buffer.data() + buffer.size());
}
```

指针去重的核心在 `SerializeNode`([src/ir/serialization/serializer.cpp:167-192](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/serialization/serializer.cpp#L167-L192)):

```cpp
auto it = ptr_to_id_.find(node.get());
if (it != ptr_to_id_.end()) {
  // 已经写过这个对象 → 只输出引用
  ref_map["ref"] = msgpack::object(it->second, zone);
  return msgpack::object(ref_map, zone);
}
uint64_t id = next_id_++;
ptr_to_id_[node.get()] = id;          // 登记在写字段之前,环也能处理
node_map["id"]   = msgpack::object(id, zone);
node_map["type"] = msgpack::object(node->TypeName(), zone);
node_map["fields"] = SerializeFields(node, zone);
```

注意一个细节:**先把指针登记进 `ptr_to_id_`,再递归序列化字段**。这样即使 IR 里出现环(理论上不该有,但防御性地),引用也能正确落回已登记的 id,不会无限递归。

字段怎么写?按节点的具体 `ObjectKind` 分派,再交给反射式的 `SerializeFieldsGeneric`([src/ir/serialization/serializer.cpp:194-246](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/serialization/serializer.cpp#L194-L246)):

```cpp
SERIALIZE_FIELDS(MemRef);
SERIALIZE_FIELDS(IterArg);
SERIALIZE_FIELDS(Var);
...
SERIALIZE_FIELDS(Call);
SERIALIZE_FIELDS(Submit);
// BinaryExpr/UnaryExpr 是抽象基类,用 dynamic_pointer_cast
SERIALIZE_FIELDS_BASE(BinaryExpr);
SERIALIZE_FIELDS_BASE(UnaryExpr);
...
SERIALIZE_FIELDS(Function);
SERIALIZE_FIELDS(Program);
```

这串清单与 u4-l1 讲过的 IR 节点层级一一对应——新增一种 IR 节点而忘了登记这里,`INTERNAL_UNREACHABLE_SPAN` 会在序列化时立刻报错,而不是静默产出残缺产物。

普通字段之外,`Call` 的 `kwargs_`/属性里还可能塞着 **IR 节点类型的属性值**(如 `task_id_var` 是 `VarPtr`、`manual_dep_edges` 是 `std::vector<VarPtr>`)。它们不走叶子字段的序列化,而是复用同一套节点引用机制([src/ir/serialization/serializer.cpp:832-856](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/serialization/serializer.cpp#L832-L856)):

```cpp
} else if (value.type() == typeid(VarPtr)) {
  // 保留 Var 按身份往返:后续引用同一 Var 的属性会写 {"ref": id}
  var_map["type"] = msgpack::object("Var", zone_);
  var_map["value"] = var ? ctx_.SerializeNode(var, zone_) : msgpack::object();
  ...
} else if (value.type() == typeid(std::vector<VarPtr>)) {
  // kAttrManualDepEdges 等 Var 列表,逐项走节点引用
  var_list_map["type"] = msgpack::object("VarList", zone_);
  ...
```

这一点与结构化比较有一条**保持同步的不变量**(官方文档原话):序列化器的属性类型梯子必须覆盖 `structural_equal` 会比较到的每一种类型——多序列化一种类型而不支持比较,或反过来,都会让某个属性「往返成功但判不了等」。

#### 4.1.4 代码实践

**实践目标**:亲手做一次表达式级序列化,并验证指针共享被保留。

操作步骤(参照 [tests/ut/ir/transforms/test_serialization.py:43-54](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/ir/transforms/test_serialization.py#L43-L54) 的写法,示例代码):

```python
from pypto import DataType, ir

span = ir.Span.unknown()
x = ir.Var("x", ir.ScalarType(DataType.INT64), span)
expr = ir.Add(x, x, DataType.INT64, span)   # 同一个 x 出现两次

data = ir.serialize(expr)
print(type(data), len(data))                # bytes,几十字节

restored = ir.deserialize(data)
# 关键断言:两次引用还原为同一个对象
assert restored.left is restored.right
ir.assert_structural_equal(expr, restored, enable_auto_mapping=True)
```

观察与预期:`restored.left is restored.right` 为真——字节流里第二个 `x` 只是一个 `{"ref": 0}`,反序列化时两边解析到同一个 `VarPtr`。若把第二处换成另一个独立构造的 `ir.Var`,该断言应为假。具体字节数与字段布局待本地验证(可用 `import msgpack; msgpack.unpackb(data)` 逐层查看)。

#### 4.1.5 小练习与答案

**练习 1**:为什么 `ptr_to_id_` 要在递归序列化字段**之前**登记当前指针?

答案:保证自引用/环状结构终止——登记后,字段里再遇到同一指针时查表命中,写出 `{"ref": id}` 而不再递归;同时也保证任意 DAG 中同一对象只被完整序列化一次。

**练习 2**:`ir.serialize` 同一个节点两次,两次字节流一定逐字节相同吗?两次反序列化的对象之间 `is` 判等成立吗?

答案:在节点与遍历次序确定、且不含按指针身份区分的内容时字节流确定(`id` 从 0 顺序分配);但两次反序列化各建一套全新的 C++ 对象,跨调用之间**没有**指针相等性——指针共享只在单次序列化/反序列化内部成立。这也是 4.3 节往返比较要开自动映射的原因之一。

### 4.2 反序列化器:从字节流重建 IR 图

#### 4.2.1 概念说明

反序列化是序列化的镜像:查 `id_to_ptr_` 表把 `{"ref": id}` 还原成已构造的对象,把完整节点交给**类型注册表**(TypeRegistry)按 `type` 名字分派到对应的构造函数。它的难点不在「造节点」,而在**恢复身份**——尤其是 MemRef 的分配身份。

#### 4.2.2 核心流程

```text
bytes → msgpack::unpack
  → DeserializeNode(obj)
      ├─ 是 {"ref": id}? → 查 id_to_ptr_,返回已构造的对象
      ├─ 读出 id / type / fields
      ├─ TypeRegistry::Create(type_name, fields, ...)   ← 按名字分派构造函数
      └─ id_to_ptr_[id] = node                          ← 同样先登记再返回
```

#### 4.2.3 源码精读

入口与错误兜底([src/ir/serialization/deserializer.cpp:53-65](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/serialization/deserializer.cpp#L53-L65)):MessagePack 的解析/类型错误被翻译成 PyPTO 自己的 `RuntimeError` 抛给 Python。

引用还原与类型注册表([src/ir/serialization/deserializer.cpp:67-122](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/serialization/deserializer.cpp#L67-L122)):

```cpp
if (key == "ref") {
  uint64_t id; p->val.convert(id);
  auto it = id_to_ptr_.find(id);
  CHECK(it != id_to_ptr_.end()) << "Invalid reference ID: " << id;   // 用户数据 → CHECK
  return it->second;
}
...
IRNodePtr node = TypeRegistry::Instance().Create(type_name, fields_obj, zone, *this);
id_to_ptr_[id] = node;   // 造完即登记
```

注意这里引用错乱走的是 `CHECK`(数据来自外部的字节流,属于用户输入错误),而缺 `id/type/fields` 这类「本模块自己写出来的东西不该缺」走 `INTERNAL_CHECK`——正是 error-checking 规则的活例子。

**分配身份的恢复**是反序列化里最微妙的一段。MemRef 序列化时同时写了 `base`(名字)与 `base_node`(基地址指针 `Var` 本体);反序列化优先用节点([src/ir/serialization/deserializer.cpp:204-223](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/serialization/deserializer.cpp#L204-L223)):

```cpp
// 分配身份是 base_ 的指针身份(MemRef::SameAllocation、地址分配器的
// base 分组……),按名字重建出来的 Var 与 alloc 语句定义的 Ptr 对不上,
// 分组就会按多块分配来计大小。
VarPtr base = base_node;
if (!base) {
  // 旧格式没有 base_node:按名字驻留(interning)一个 Var,恢复 MemRef 之间的共享
  auto& interned = memref_bases_[base_name];
  if (!interned) interned = std::make_shared<Var>(base_name, GetPtrType(), Span::unknown());
  base = interned;
}
```

一句话:**新格式靠节点图保身份,旧格式靠名字驻留尽量止损**。这也解释了 4.1 节为什么强调 `{"ref": id}` 机制——它是分配身份能活过序列化的根。

#### 4.2.4 代码实践

**实践目标**:走一遍文件级往返,并理解往返等价的判据。

操作步骤(示例代码,样板来自 [tests/ut/ir/transforms/test_serialization.py:36-40](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/ir/transforms/test_serialization.py#L36-L40) 的 `_round_trip_type`):

```python
import pypto.language as pl
from pypto import ir

@pl.program
class P:
    @pl.function
    def main(self, x: pl.Tensor[[64, 64], pl.FP32]) -> pl.Tensor[[64, 64], pl.FP32]:
        y = x + x
        return y

ir.serialize_to_file(P, "/tmp/p.msgpack")
restored = ir.deserialize_from_file("/tmp/p.msgpack")
ir.assert_structural_equal(restored, P, enable_auto_mapping=True)
```

观察与预期:断言通过;`restored` 与 `P` 是两套独立对象(指针不同),但结构一致。函数级属性(如 `pl.func_attr` 写入的 Var 值属性)同样按身份往返——参照真实测试 [tests/ut/language/parser/test_func_attr.py:103-121](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/language/parser/test_func_attr.py#L103-L121),该测试还额外断言 `func.attrs["stationary"]` 解析回**参数本身**而不是一个克隆。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**:往返比较为什么传 `enable_auto_mapping=True`,而 Pass 的 before/after 测试默认 `False` 就够?

答案:反序列化产物里的 Var 全是**新指针**,与原 IR 的 Var 没有任何指针相等关系,只能靠自动映射把两棵树的变量一一配对。Pass 测试两边是同一份源 IR 经变换/复制的产物:定义点(AssignStmt 的 `var_` 等)是 DefField,比较时**强制**开启自动映射并填充双向映射表,后续使用点查表即中,因此默认 False 即可(官方文档「When to Enable Auto-Mapping」一节给出的正是这张对照表)。

**练习 2**:旧格式 blob(没有 `base_node`)反序列化后,MemRef 的分配身份会发生什么?

答案:退化为「按 base 名字驻留」——同名 base 的所有 MemRef 共享一个新建的 Var,MemRef 之间仍在一块分配上;但这个 Var 不是 alloc 语句定义的那个 Ptr,凡是指针身份参与的判断(地址分配器的 base 分组等)都会把它当成另一块分配,分组大小会被高估。

### 4.3 节点级结构化相等:structural_equal / assert_structural_equal

#### 4.3.1 概念说明

`structural_equal` 与 `assert_structural_equal` 是同一个模板类的两种 instantiation:前者失配返回 `false`,后者失配抛带定位信息的 `ValueError`。它们的比较规则可以一句话概括:**指针不同但字段全同 → 相等;Span 与名字类字段永远视为相等;变量按映射表判**。

变量之所以特殊,是因为「同一个变量」在比较中是**相对概念**:比较两棵不同的树时,lhs 的 `x` 对应 rhs 的哪个变量,需要在比较过程中逐步建立并保持一致(双向映射,防 `x→y` 又 `x→z` 的矛盾)。

#### 4.3.2 核心流程

```text
Equal(lhs, rhs)
  ├─ 指针相同 → true(快路径)
  ├─ 任一为空 / TypeName 不同 → false
  ├─ MemRef? → EqualMemRef(先按 Var 比较,再比 base/byte_offset/size)
  ├─ IterArg? WindowBuffer? → 各自专用比较
  ├─ Var? → EqualVar(映射表逻辑,见下)
  └─ 其余 → EqualWithFields:取 GetFieldDescriptors(),逐字段访问
        ├─ IgnoreField → 跳过(Span、name_hint 等)
        ├─ DefField   → 临时强制 enable_auto_mapping = true
        └─ UsualField → 按调用方传入的 enable_auto_mapping
```

`EqualVar` 的映射逻辑([src/ir/transforms/structural_equal.cpp:1311-1381](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/structural_equal.cpp#L1311-L1381))分两档:

- **不开自动映射**:先查双向映射表(可能已被 DefField 填过),命中且一致 → true;否则退回严格指针比较,指针不同即 false。
- **开自动映射**:先比类型,再查表——lhs 已映射则必须映射到 rhs;rhs 已被别的 lhs 占用则拒绝;两边都没记录就新建映射 `lhs→rhs`、`rhs→lhs`。双向表保证映射是单射,`x+x` 对 `y+y` 为真、对 `y+z` 为假。

#### 4.3.3 源码精读

模板类与模式开关([src/ir/transforms/structural_equal.cpp:57-84](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/structural_equal.cpp#L57-L84)):`AssertMode=false` 返回布尔,`AssertMode=true` 抛异常,一套比较逻辑两用。

三类字段钩子([src/ir/transforms/structural_equal.cpp:731-748](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/structural_equal.cpp#L731-L748)):

```cpp
template <typename FVisitOp>
void VisitIgnoreField(FVisitOp&&) {
  // 忽略字段永远相等(Span、name_hint 等)
}
template <typename FVisitOp>
void VisitDefField(FVisitOp&& visit_op) {
  bool enable_auto_mapping = true;
  std::swap(enable_auto_mapping, enable_auto_mapping_);   // 临时强制开
  visit_op();
  std::swap(enable_auto_mapping, enable_auto_mapping_);   // 用完还原
}
```

这就是练习 4.2-1 答案的代码依据:**定义点永远按自动映射比较**,与调用方的开关无关。

泛型字段比较([src/ir/transforms/structural_equal.cpp:808-819](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/structural_equal.cpp#L808-L819)):

```cpp
template <typename NodePtr>
bool EqualWithFields(const NodePtr& lhs_op, const NodePtr& rhs_op) {
  auto descriptors = NodeType::GetFieldDescriptors();
  return std::apply([&](auto&&... descs) {
    return reflection::FieldIterator<NodeType, StructuralEqualImpl<AssertMode>,
                                     decltype(descs)...>::Visit(*lhs_op, *rhs_op, *this, descs...);
  }, descriptors);
}
```

与 u4-l6 讲过的算子注册表一样,这里靠**字段描述符 + 访问器**做到「加新节点不用改比较器」——比较器只认识字段种类,不认识具体节点类型。节点分派入口在 [src/ir/transforms/structural_equal.cpp:914-954](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/structural_equal.cpp#L914-L954),注意它**先判 MemRef、再判 IterArg、再判 WindowBuffer、最后判 Var**——这三者都继承自 Var 但各有专属 ObjectKind(`As<T>()` 精确匹配),顺序错不了,只是可读性的显式声明(参见 ir-kind-traits 规则)。

断言模式的报错([src/ir/transforms/structural_equal.cpp:822-872](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/structural_equal.cpp#L822-L872))是这一对 API 里最实用的部分:失配时用 `path_` 记录的字段路径(如 `body[1].value.left`)加上两侧节点的 `PythonPrint` 输出,拼成一条 `ValueError`。也就是说,**assert 版本不只告诉你「不等」,还告诉你第一个失配点在哪、两边各长什么样**——这是 Pass 测试调试时的主要信息来源。

公共 API 四个重载([src/ir/transforms/structural_equal.cpp:1517-1537](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/structural_equal.cpp#L1517-L1537)),Python 侧经 [python/bindings/modules/ir.cpp:1326-1361](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/bindings/modules/ir.cpp#L1326-L1361) 暴露为 `ir.structural_equal` / `ir.assert_structural_equal` / `ir.structural_hash`,`enable_auto_mapping` 默认 `False`。

#### 4.3.4 代码实践

**实践目标**:让比较器「抓到」一个故意的差异,并读懂报错。

操作步骤(示例代码):

```python
import pypto.language as pl
from pypto import ir

@pl.program
class A:
    @pl.function
    def main(self, x: pl.Tensor[[64, 64], pl.FP32]) -> pl.Tensor[[64, 64], pl.FP32]:
        return x + x

@pl.program
class B:
    @pl.function
    def main(self, x: pl.Tensor[[64, 64], pl.FP32]) -> pl.Tensor[[64, 64], pl.FP32]:
        return x * x          # 只有这里不同

ir.assert_structural_equal(A, B)   # 预期抛 ValueError
```

观察与预期:抛出 `ValueError`,消息里包含第一个失配字段的路径(形如 `functions['main'].body...` 加节点下标)以及两侧的 Python 打印,`Reason` 一栏指出算子或字段值不匹配;把 `B` 改回 `x + x` 后断言通过。具体路径文案待本地验证。

#### 4.3.5 小练习与答案

**练习 1**:`structural_equal` 为什么忽略 Span?

答案:Span 记录源码位置,是调试信息而非语义。两个 Pass 前后的 IR、或打印再解析的 IR,语句相同但位置几乎必然不同;比较若包含 Span,几乎所有合法变换都会被误判为不等。

**练习 2**:同一个比较任务,什么时候用 `structural_equal`、什么时候用 `assert_structural_equal`?

答案:测试断言与任何「不等就必须停下」的场景用 assert 版——失配时有路径与两侧打印,可直接定位;做查找/过滤(例如在一批函数里挑出与模板相同的)用布尔版本。二者是同一模板的两种 instantiation,比较逻辑完全一致,不会出现「一个认为等、另一个认为不等」。

### 4.4 AreExprsEqual 的比较粒度,以及哈希为什么必须同步

#### 4.4.1 概念说明

节点级 `structural_equal` 处理整棵 IR;但在**类型内部**,还有一层更细的比较需求。`TileView`(u4-l4 讲过:Tile 的有效区/步长/紧凑表示视图)的字段是 `std::vector<ExprPtr>`——比较两个 TileView 就是比较两组表达式。这些表达式没有走字段描述符体系,而是走一个专用函数 `AreExprsEqual`,它的比较粒度是**手工选择的**:

| 表达式种类 | 比较方式 |
| ---------- | -------- |
| `ConstInt` | 按**值**比较(两个独立构造的 42 相等) |
| `BinaryExpr`(Add/Mul/…) | 按**结构**:同 kind + 左右递归相等 |
| `UnaryExpr`(Cast 等) | 按**结构**:同 kind + 结果 dtype 相同 + 操作数递归相等(**本版本新增**) |
| `Call` | 按**算子名 + 实参**递归(不同调用点的两个 `pld.world_size()` 相等) |
| 其余(Var 等) | 按**指针**身份(保守:两个同名的 Var 不相等) |

为什么不全按结构?因为 Var 是**带身份**的东西:名字相同不代表是同一个变量(SSA 改名后尤其如此),保守地按指针判等把「不确定」当成「不等」,宁可漏合并不可错合并。

#### 4.4.2 核心流程

比较一条表达式的决策树:

```text
AreExprsEqual(e1, e2)
  ├─ 同一指针 → true
  ├─ 都是 ConstInt → 值相等?
  ├─ 都是 BinaryExpr 且同 kind → 左右递归
  ├─ 都是 UnaryExpr 且同 kind → 结果 dtype 相等? → 操作数递归   ← 新增
  ├─ 都是 Call 且算子名相同 → 逐实参递归
  └─ 其余 → false(指针不同即不等)
```

配套的哈希 `HashExprForAreExprsEqual` 用**标签位**区分五类,保证值相等、结构相等、指针相等的三种「相等」不会巧合地落进同一个桶:

\[ \mathrm{combine}(seed, v) = seed \oplus \left( v + \mathtt{0x9e3779b9} + (seed \ll 6) + (seed \gg 2) \right) \]

标签取值:`kConstIntHashTag=1`、`kExprPtrHashTag=2`、`kBinaryExprHashTag=3`、`kCallExprHashTag=4`、`kUnaryExprHashTag=5`(**新增**)。

#### 4.4.3 源码精读

`AreExprsEqual` 全貌([src/ir/expr.cpp:69-112](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/expr.cpp#L69-L112))。本版本(c7ba9fb → ec5d20c)的变化是**新增了一元表达式分支**:

```cpp
// Unary nodes compare the same way, plus their result dtype. `Cast` is the one
// that matters in practice: a declared allocation's runtime slot subscript
// lowers to `cast(index, INDEX) % n * stride`, and two sites that write the
// same subscript build two such trees. ...
auto u1 = As<UnaryExpr>(e1);
auto u2 = As<UnaryExpr>(e2);
if (u1 && u2 && e1->GetKind() == e2->GetKind()) {
  return ScalarDataTypeOf(e1) == ScalarDataTypeOf(e2) && AreExprsEqual(u1->operand_, u2->operand_);
}
```

([src/ir/expr.cpp:82-96](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/expr.cpp#L82-L96),辅助函数 `ScalarDataTypeOf` 在 [src/ir/expr.cpp:58-67](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/expr.cpp#L58-L67))

两个工程要点值得咀嚼:

1. **动机**:声明式分配的运行时槽位下标会降级成 `cast(index, INDEX) % n * stride` 这样的树;两个写同一槽位的代码点各自构造出一棵这样的树。按指针比较会把它们判为不等,内存规划就会误以为「携带值换了槽位」,其实它从没离开过那个槽。
2. **dtype 必须参与比较**:`cast(x, INT32)` 与 `cast(x, INDEX)` 同 kind、同操作数,但表示**不同的数值**;不比 dtype,按字节偏移比较的调用方会把两个不同地址读成同一个。

Call 分支按**算子名**而非指针比较([src/ir/expr.cpp:97-110](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/expr.cpp#L97-L110)),注释点名了动机:反序列化之后两个 Op 是不同对象但共享名字——这正是 u4-l6「算子身份是名字而非指针」不变量在这里的回声。

消费方是 `TileView` 的相等与哈希([src/ir/type.cpp:105-110](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/type.cpp#L105-L110)):

```cpp
bool operator==(const TileView& lhs, const TileView& rhs) {
  return AreExprVectorsEqual(lhs.valid_shape, rhs.valid_shape) &&
         AreExprVectorsEqual(lhs.stride, rhs.stride) && AreExprsEqual(lhs.start_offset, rhs.start_offset) &&
         lhs.blayout == rhs.blayout && ... && lhs.compact == rhs.compact;
}
```

哈希侧的镜像分支([src/ir/type.cpp:130-157](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/type.cpp#L130-L157)):

```cpp
if (auto u = As<UnaryExpr>(e)) {
  // AreExprsEqual 比较一元节点的 kind、结果 dtype、操作数,
  // 三者都得进哈希,否则两个相等的 TileView 会落进不同的桶
  uint64_t h = hash_combine(kUnaryExprHashTag, static_cast<uint64_t>(e->GetKind()));
  auto scalar = std::dynamic_pointer_cast<const ScalarType>(e->GetType());
  h = hash_combine(h, scalar ? static_cast<uint64_t>(scalar->dtype_.Code()) : 0);
  return hash_combine(h, HashExprForAreExprsEqual(u->operand_));
}
```

文件内的注释([src/ir/type.cpp:125-129](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/type.cpp#L125-L129))把维护契约写得很直白:**「`AreExprsEqual` 的任何扩展必须在这里得到对应分支」**。

为什么必须同步?哈希与相等是容器协议的两个半边。设 `Hash` 漏掉了 dtype:

- `eq(cast(x, INDEX), cast(x, INT64))` = false(不等,正确);
- 但 `hash(cast(x, INDEX))` 与 `hash(cast(x, INT64))` **可能相同**——这本身合法(哈希允许碰撞)。

真正的破坏方向是**相等而哈希不等**:若 `eq` 认为两个视图相等(新增的一元分支生效),而 `hash` 还按指针混入地址(旧版没有一元分支),则两个「相等」的视图哈希不同,`set`/`dict` 里 `{v1, v2}` 不肯坍缩成一个元素——这正是 [tests/ut/ir/core/test_tile_view_equality.py:200-231](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/ir/core/test_tile_view_equality.py#L200-L231) 里 `TestUnaryExprEquality` 断言的三件事:

```python
left  = ir.cast(operand, DataType.INDEX, span)
right = ir.cast(operand, DataType.INDEX, span)   # 两个不同对象
assert lhs == rhs
assert hash(lhs) == hash(rhs)
assert len({lhs, rhs}) == 1    # 相等的视图在集合里必须坍缩
```

而 dtype 参与比较的对照组在同文件 [tests/ut/ir/core/test_tile_view_equality.py:233-251](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/ir/core/test_tile_view_equality.py#L233-L251):同一操作数分别 cast 到 `INDEX` 与 `INT64` 的两个视图**不等且哈希不同**;外加一个「同操作数同 dtype 仍相等」的对照,防止「全都不等」这种假通过。

顺带把节点级哈希的身份来源也说清:`structural_hash` 对自由变量按 `Var::UniqueId()`(构造时原子递增分配的序号,见 [include/pypto/ir/expr.h:229-256](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/expr.h#L229-L256))参与哈希而非指针地址或名字([src/ir/transforms/structural_hash.cpp:677-689](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/structural_hash.cpp#L677-L689))——所以哈希在同一进程内是确定性的:同样的构造顺序产出同样的哈希。

#### 4.4.4 代码实践

**实践目标**:亲手复现「一元表达式按结构比较、哈希与相等同步」这两件事。

操作步骤(示例代码,即 `TestUnaryExprEquality` 的最小化重写):

```python
from pypto import DataType, ir

span = ir.Span.unknown()
operand = ir.Var("x", ir.ScalarType(DataType.INT32), span)

# ① 同操作数、同 dtype 的两个 cast:不同对象,但按结构相等
c1 = ir.cast(operand, DataType.INDEX, span)
c2 = ir.cast(operand, DataType.INDEX, span)
tv1 = ir.TileView([c1, ir.ConstInt(16, DataType.INT64, span)],
                  [ir.ConstInt(1, DataType.INT64, span)])
tv2 = ir.TileView([c2, ir.ConstInt(16, DataType.INT64, span)],
                  [ir.ConstInt(1, DataType.INT64, span)])
assert tv1 == tv2 and hash(tv1) == hash(tv2) and len({tv1, tv2}) == 1

# ② 同操作数、不同 dtype:不等,哈希也不同
tv3 = ir.TileView([ir.cast(operand, DataType.INT64, span),
                   ir.ConstInt(16, DataType.INT64, span)],
                  [ir.ConstInt(1, DataType.INT64, span)])
assert tv1 != tv3 and hash(tv1) != hash(tv3)

# ③ 两个独立构造的同名 Var:按指针判,保守地不等
v1 = ir.Var("M", ir.ScalarType(DataType.INT64), span)
v2 = ir.Var("M", ir.ScalarType(DataType.INT64), span)
assert not ir.structural_equal(
    ir.TileView([v1], [ir.ConstInt(1, DataType.INT64, span)]),
    ir.TileView([v2], [ir.ConstInt(1, DataType.INT64, span)]),
)
```

观察与预期:①②③ 全部通过;①的三个断言分别验证「相等」「哈希一致」「集合坍缩」三件事。若把 ② 中 `INT64` 改成 `INDEX`,则回到 ① 的情形。整段即回归测试的浓缩,也可直接运行原测试验证:`python -m pytest tests/ut/ir/core/test_tile_view_equality.py -v`(需按 u1-l2 完成构建后执行,具体输出待本地验证)。

#### 4.4.5 小练习与答案

**练习 1**:为什么 `AreExprsEqual` 里 Var 按指针、ConstInt 按值,而不是统一处理?

答案:ConstInt 是**纯值**——两个 42 没有任何可区分的身份;Var 是**带身份的绑定**,同名不同对象可能是不同变量(SSA 版本),错合并会导致错误的 CSE/内存合并。粒度选择的准则:能从结构完全确定语义的(ConstInt、二元/一元运算、按名字注册的算子 Call)按结构比;语义依赖分配身份的(Var)保守按指针比。

**练习 2**:假如只给 `AreExprsEqual` 加一元分支、忘了给 `HashExprForAreExprsEqual` 加,哪个方向会坏——「相等的判不等」还是「相等的哈希不同」?

答案:后者。`==` 会把两棵同构的 cast 树判等,但 `hash` 走旧分支按指针地址混入,两个相等的 TileView 哈希不同,`set`/`dict` 不再把它们当同一个键(集合不坍缩、缓存永远未命中)。这就是两处粒度必须一比一同步的原因——代码注释里「any extension to AreExprsEqual MUST get a corresponding branch here」说的正是这个契约。

**练习 3**:`cast(x, INT32)` 与 `cast(x, INDEX)` 为什么不能只比 kind 和操作数?

答案:cast 的结果 dtype 是值的一部分且**不能从操作数推导**——同一操作数 cast 到不同 dtype 表示不同的数值(字节宽度、地址运算语义都不同)。调用方(如按字节偏移比较的内存规划)会把它们读成同一个地址,造成错误合并。

## 5. 综合实践

把四个模块串成一个完整的「往返验证脚本」。任务:验证一个真实 Program 的序列化往返等价,故意制造差异确认比较器能抓到,最后用一段话解释 AreExprsEqual 与 HashExprForAreExprsEqual 的同步契约。

```python
"""u4-l8 综合实践:序列化往返 + 差异检测(示例代码)"""
import pypto.language as pl
from pypto import DataType, ir

# ---- 第 1 步:构造 Program(编译器持有的同一棵 IR)----
@pl.program
class P:
    @pl.function
    def main(self, x: pl.Tensor[[64, 64], pl.FP32]) -> pl.Tensor[[64, 64], pl.FP32]:
        return x + x

# ---- 第 2 步:二进制往返,断言结构等价 ----
restored = ir.deserialize(ir.serialize(P))
ir.assert_structural_equal(restored, P, enable_auto_mapping=True)
print("round-trip OK")

# ---- 第 3 步:故意改一个字段,比较器必须报出差异 ----
@pl.program
class P2:
    @pl.function
    def main(self, x: pl.Tensor[[64, 64], pl.FP32]) -> pl.Tensor[[64, 64], pl.FP32]:
        return x * x                     # add → mul

try:
    ir.assert_structural_equal(restored, P2, enable_auto_mapping=True)
    raise SystemExit("FAIL: 差异未被检出")
except ValueError as e:
    print("comparator caught the diff:\n", str(e)[:400])

# ---- 第 4 步:表达式级粒度——cast 按结构比、Var 按指针比 ----
span = ir.Span.unknown()
op = ir.Var("x", ir.ScalarType(DataType.INT32), span)
a = ir.TileView([ir.cast(op, DataType.INDEX, span)])
b = ir.TileView([ir.cast(op, DataType.INDEX, span)])
assert a == b and hash(a) == hash(b)

# ---- 第 5 步:用自己的话写下同步契约 ----
# 「AreExprsEqual 的每一次粒度扩展,必须在 HashExprForAreExprsEqual 里
#   得到一个标签互异、字段相同的镜像分支,否则『相等但哈希不同』会让
#   以哈希为键的容器不再把相等对象视为同一个键。」
```

验收标准:

1. 第 2 步断言通过(往返等价)。
2. 第 3 步 `ValueError` 被捕获,且消息中能找到失配路径与两侧打印。
3. 第 4 步三个断言通过。
4. 能口头回答:为什么第 2 步传 `enable_auto_mapping=True`?(4.2.5 练习 1 的答案)

运行前提:按 u1-l2 完成构建并 `import pypto` 可用;现象描述中的具体报错文案待本地验证。

## 6. 本讲小结

- 序列化把 IR 图写成 MessagePack:每个对象带递增 `id` 只写一次,后续引用写 `{"ref": id}`,指针共享(包括 MemRef 的分配身份)因此能活过序列化;反序列化靠 `id_to_ptr_` 与类型注册表重建,旧格式 blob 靠按名驻留止损。
- 往返等价的裁判是 `assert_structural_equal`:忽略 Span 与名字,按字段描述符递归比较;IgnoreField 永远相等,DefField 强制开启自动映射,UsualField 跟随调用方开关;断言模式失配时给出字段路径 + 两侧打印。
- 往返测试开 `enable_auto_mapping=True`(反序列化的 Var 全是新指针),Pass 的 before/after 测试用默认 False(定义点已填充映射表)。
- 表达式级的 `AreExprsEqual` 是手工选粒度的比较器:ConstInt 按值、二元/一元/Call 按结构、其余按指针;本版本新增一元(Cast)分支——同 kind + 同结果 dtype + 操作数递归相等,动机是声明式分配的槽位下标 `cast(index, INDEX) % n * stride` 需要跨调用点判等。
- `HashExprForAreExprsEqual` 必须与 `AreExprsEqual` 一比一同步(新增 `kUnaryExprHashTag=5` 分支):否则「相等但哈希不同」会让 set/dict/CSE 缓存把相等对象当成两个键——`TestUnaryExprEquality` 用「相等、哈希同、集合坍缩」三条断言把这个契约钉死。
- 同步契约不止这一处:官方文档列出 Type 的四个手工梯子(Equal/Hash/Serialize/Deserialize 各一份),新增 Type 要四处齐改,漏改 HashType 会破坏「相等必哈希相等」。

## 7. 下一步学习建议

- **u5-l1(PassContext 与验证器体系)**:本讲的 `assert_structural_equal` 是测试期裁判,下一讲会看到流水线运行期的 PropertyVerifier 体系,两者共同守护 IR 不变量。
- **u7-l7(测试体系与结构化断言)**:把本讲的比较器放进 before/after 测试范式的工程实践中,学习如何为自己的 Pass/算子写结构化回归测试。
- 继续阅读源码:在 `src/ir/transforms/` 下 `grep -rn "AreExprsEqual"` 观察这个粒度选择被哪些 Pass 依赖(内存规划、布局降级等),加深对「比较粒度即合并安全性」的理解。
