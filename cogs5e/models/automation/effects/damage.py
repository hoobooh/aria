import copy

import d20
import draconic

from cogs5e.models.sheet.resistance import Resistances, do_resistances
from utils.constants import TOUGHNESS_MAP
from utils.enums import CritDamageType
from . import Effect
from .roll import RollEffectMetaVar
from .. import utils
from ..errors import TargetException
from ..results import DamageResult
from ...sheet.attributes import Attributes


class Damage(Effect):
    def __init__(
            self,
            damage: str,
            overheal: bool = False,
            higher: dict = None,
            cantripScale: bool = None,
            fixedValue: bool = None,
            **kwargs,
    ):
        super().__init__("damage", **kwargs)
        self.damage = damage
        self.overheal = overheal
        # common
        self.higher = higher
        self.cantripScale = cantripScale
        self.fixedValue = fixedValue

    def to_dict(self):
        out = super().to_dict()
        out.update({"damage": self.damage, "overheal": self.overheal})
        if self.higher is not None:
            out["higher"] = self.higher
        if self.cantripScale is not None:
            out["cantripScale"] = self.cantripScale
        if self.fixedValue is not None:
            out["fixedValue"] = self.fixedValue
        return out

    def run(self, autoctx):
        super().run(autoctx)
        if autoctx.target is None:
            raise TargetException(
                "Tried to do damage without a target! Make sure all Damage effects are inside of a Target effect."
            )
        # general arguments
        args = autoctx.args
        damage = self.damage
        resistances = Resistances()
        attributes = Attributes()
        c_args = args.get("c", [], ephem=True)
        crit_arg = args.last("crit", None, bool, ephem=True)
        nocrit = args.last("nocrit", default=False, type_=bool, ephem=True)
        max_arg = args.last("max", None, bool, ephem=True)
        magic_arg = args.last("magical", None, bool, ephem=True)
        silvered_arg = args.last("silvered", None, bool, ephem=True)
        mi_arg = args.last("mi", None, int)
        dtype_args = args.get("dtype", [], ephem=True)
        critdice = sum(args.get("critdice", type_=int))
        savage = args.last("savage", None, bool, ephem=True)
        hide = args.last("h", type_=bool)

        crit_damage_type = autoctx.crit_type

        # character-specific arguments
        if autoctx.character and args.last("critdice") is None:
            critdice = autoctx.character.options.extra_crit_dice

        # combat-specific arguments
        if not autoctx.target.is_simple:
            resistances = autoctx.target.get_resists().copy()
            attributes = autoctx.target.get_attributes().copy()
        resistances.update(Resistances.from_args(args, ephem=True))

        tmpstring = ""

        # check if we actually need to run this damage roll (not in combat and roll is redundant)
        if autoctx.target.is_simple and self.is_meta(autoctx):
            return

        d_args = []
        # check if we actually need to care about the -d tag
        if not (self.contains_roll_meta(autoctx) or self.fixedValue):
            d_args = args.get("d", [], ephem=True)
            # add on combatant damage effects (#224)
            d_args.extend(autoctx.caster_active_effects(mapper=lambda effect: effect.effects.damage_bonus, default=[]))

        # set up damage AST

        damage = autoctx.parse_annostr(damage)
        dice_ast = copy.copy(d20.parse(damage))
        dice_ast = utils.upcast_scaled_dice(self, autoctx, dice_ast)

        if savage:
            dice_ast.roll = d20.ast.OperatedSet(
                d20.ast.NumberSet([dice_ast.roll, dice_ast.roll]), d20.SetOperator("k", [d20.SetSelector("h", 1)])
            )

        # -mi # (#527)
        if mi_arg:
            dice_ast = d20.utils.tree_map(utils.mi_mapper(mi_arg), dice_ast)

        # -d #
        for d_arg in d_args:
            d_ast = d20.parse(d_arg)
            dice_ast.roll = d20.ast.BinOp(dice_ast.roll, "+", d_ast.roll)

        # apply s.attribute bonuses here to damage
        tempdmg = autoctx.caster.attributes.get_damage_with_bonus(str(dice_ast.roll))
        if tempdmg != "":
            d_ast = d20.parse(tempdmg)
            dice_ast.roll = d20.ast.BinOp(dice_ast.roll, "+", d_ast.roll)

        # dice_ast += autoctx.caster.attributes.get_damage_with_bonus(damage)

        # crit
        # nocrit (#1216)
        # Disable critical damage in saves (#1556)
        in_crit = (autoctx.in_crit or crit_arg) and not (nocrit or autoctx.in_save)
        if in_crit:
            if crit_damage_type == CritDamageType.MAX_ADD:
                dice_ast = utils.tree_map_prefix(utils.max_add_crit_mapper, dice_ast)
            elif crit_damage_type == CritDamageType.DOUBLE_ALL:
                dice_ast.roll = d20.ast.BinOp(d20.ast.Parenthetical(dice_ast.roll), "*", d20.ast.Literal(2))
            elif crit_damage_type == CritDamageType.DOUBLE_DICE:
                dice_ast = utils.tree_map_prefix(utils.double_dice_crit_mapper, dice_ast)
            else:
                dice_ast = d20.utils.tree_map(utils.crit_mapper, dice_ast)
            if critdice and not autoctx.is_spell:
                if crit_damage_type in (CritDamageType.DOUBLE_ALL, CritDamageType.DOUBLE_DICE):
                    crit_ast = utils.crit_dice_gen(dice_ast, critdice)
                    if crit_ast:
                        dice_ast.roll = d20.ast.BinOp(dice_ast.roll, "+", crit_ast)
                else:
                    utils.critdice_tree_update(dice_ast, int(critdice))

        # -c #
        if in_crit:
            for c_arg in c_args:
                c_ast = d20.parse(c_arg)
                dice_ast.roll = d20.ast.BinOp(dice_ast.roll, "+", c_ast.roll)

        # max
        if max_arg:
            dice_ast = d20.utils.tree_map(utils.max_mapper, dice_ast)

        # apply defenses here to damage
        tempdmg = attributes.get_damage_with_defense(str(dice_ast.roll))
        if tempdmg != "":
            d_ast = d20.parse(tempdmg)
            dice_ast.roll = d20.ast.BinOp(dice_ast.roll, "+", d_ast.roll)

        # clean up damage string
        dice_ast.roll = d20.parse(str(dice_ast.roll).replace("+ +", "+ ").replace("+ -", "- ").replace("- +", "- "))

        # evaluate damage

        # we have to fix the parsing system because the original one is garbage and misidentifies damage type
        manip = str(dice_ast.roll)
        new_roll = "+("
        for iterator in range(manip.__len__()):
            if manip[iterator] == '[':
                new_roll += ")"
            new_roll += manip[iterator]
            if manip[iterator] == ']' and iterator != manip.__len__() - 1:
                new_roll += "+("
        dice_ast.roll = d20.parse(new_roll)

        # back to actual evaluation
        dmgroll = d20.roll(dice_ast)
        p_str = d20.SimpleStringifier().stringify(dmgroll.expr)

        # collapse the damage tree to make sure that defense doesn't overflow
        class DNode:
            def __init__(self, d_type: str, val: int):
                if val < 0 and "heal" not in d_type:
                    val = 0
                self.d_type = d_type
                self.val = val

            def add_val(self, v):
                if "heal" not in self.d_type:
                    self.val = max(0, self.val + v)
                else:
                    self.val += v

        dn_list = []
        current_val = ""
        current_type = ""
        reading_type = False
        roll_max = 0
        roll_actual = 0
        for c in p_str:
            if c == '=':
                break
            if c == '[':
                reading_type = True
            elif c == ']':

                # parse value string to find total

                current_val = current_val.replace("(", " ( ")
                current_val = current_val.replace(")", " ) ")
                current_val = current_val.replace("+", " + ")

                nodes = current_val.split()
                new_val = ""
                prev_node = None
                prevprev_node = None
                between_brackets = False

                roll_cleaned = ""
                for n in nodes:
                    if n:
                        if '[' in n:
                            between_brackets=True
                            continue
                        if ']' in n:
                            between_brackets=False
                            continue
                        if not between_brackets:
                            roll_cleaned += n + " "

                for n in roll_cleaned.split(" "):
                    if n:
                        if prevprev_node:
                            if 'd' in prevprev_node or ',' in prev_node:
                                roll_actual += int(n.replace(",",""))
                        prevprev_node = prev_node
                        prev_node = n

                between_brackets = False

                for n in nodes:
                    if n:
                        if '[' in n:
                            between_brackets=True
                            continue
                        if ']' in n:
                            between_brackets=False
                            continue
                        if not between_brackets and 'd' not in n:
                            new_val += n
                        elif not between_brackets:
                            di = n.split("d")
                            num = int(di[0])
                            size_string = ""
                            for digit in di[1]:
                                if digit.isdigit():
                                    size_string += digit
                                else:
                                    break
                            size = int(size_string)
                            if num < 0:
                                size = 1
                            roll_max += num * size

                total_val = d20.roll(d20.parse(new_val)).total

                merge = False
                for dn in dn_list:
                    if dn.d_type == current_type:
                        dn.add_val(total_val)
                        merge = True
                if not merge:
                    dn_list.append(DNode(current_type, total_val))
                current_type = ""
                current_val = ""
                reading_type = False
            elif reading_type:
                current_type += c
            else:
                current_val += c

        # takes collapsed damage tree
        expr = ""
        for dn in dn_list:
            expr += " + " + str(dn.val) + " [" + dn.d_type + "]"
        expr = expr[3:]
        dmgroll = d20.roll(d20.parse(expr))

        luck_ratio = float(roll_actual) / float(roll_max)

        # magic arg (#853), magical effect (#1063)
        # silvered arg (#1544)
        always = set()
        magical_effect = autoctx.caster_active_effects(mapper=lambda effect: effect.effects.magical_damage, reducer=any)
        if magical_effect or autoctx.is_spell or magic_arg:
            always.add("magical")
        silvered_effect = autoctx.caster_active_effects(
            mapper=lambda effect: effect.effects.silvered_damage, reducer=any
        )
        if silvered_effect or silvered_arg:
            always.add("silvered")
        # dtype transforms/overrides (#876)
        transforms = {}
        for dtype in dtype_args:
            if ">" in dtype:
                *froms, to = dtype.split(">")
                for frm in froms:
                    transforms[frm.strip().lower()] = to.strip().lower()
            else:
                transforms[None] = dtype
        # display damage transforms (#1103)
        if None in transforms:
            autoctx.meta_queue(f"**Damage Type**: {transforms[None]}")
        elif transforms:
            for frm in transforms:
                autoctx.meta_queue(f"**Damage Change**: {frm} > {transforms[frm]}")

        # evaluate resistances
        do_resistances(dmgroll.expr, resistances, always, transforms)

        # determine healing/damage, stringify expr
        result = d20.MarkdownStringifier().stringify(dmgroll.expr)
        if dmgroll.total < 0 and "heal" in result:
            roll_for = "Healing"
        else:
            luck_string = ""
            if roll_max == roll_actual:
                luck_string = "Perfect Hit!"
            elif luck_ratio > 0.7:
                luck_string = "Amazing Hit!"
            elif luck_ratio > 0.5:
                luck_string = "Solid Hit!"
            elif luck_ratio > 0.3:
                luck_string = "Weak Hit!"
            else:
                luck_string = "Grazing Hit!"

            roll_for = "Damage (" + luck_string +")"

        # output
        roll_for = roll_for if not in_crit else f"{roll_for} (CRIT!)"
        if not hide:
            autoctx.queue(f"**{roll_for}**: {result}")
        else:
            d20.utils.simplify_expr(dmgroll.expr)
            autoctx.queue(f"**{roll_for}**: {d20.MarkdownStringifier().stringify(dmgroll.expr)}")
            autoctx.add_pm(str(autoctx.ctx.author.id), f"**{roll_for}**: {result}")

        if dmgroll.total < 0 and "heal" not in result:
            autoctx.target.damage(autoctx, 0, allow_overheal=self.overheal)
        else:
            autoctx.target.damage(autoctx, dmgroll.total, allow_overheal=self.overheal)

        # #1335
        autoctx.metavars["lastDamage"] = dmgroll.total
        return DamageResult(damage=dmgroll.total, damage_roll=dmgroll, in_crit=in_crit)

    def is_meta(self, autoctx):
        """Check if the damage string is completely a metavar sub."""
        return any(f"{{{v}}}" == self.damage for v in autoctx.metavars)

    def contains_roll_meta(self, autoctx):
        """Check if the damage string contains the result of a Roll effect."""
        return any(f"{{{k}}}" in self.damage for k, v in autoctx.metavars.items() if isinstance(v, RollEffectMetaVar))

    def build_str(self, caster, evaluator):
        super().build_str(caster, evaluator)
        try:
            damage = evaluator.transformed_str(self.damage)
            evaluator.builtins["lastDamage"] = damage
        except draconic.DraconicException:
            damage = self.damage
            evaluator.builtins["lastDamage"] = 0

        # damage/healing
        if damage.startswith("-"):
            return f"{damage[1:].strip()} healing"
        return f"{damage} damage"
