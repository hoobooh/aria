import dataclasses

from d20 import Dice

from cogs5e.models.errors import InvalidArgument
from .mixins import HasIntegrationMixin
from utils.constants import COIN_TYPES, TOUGHNESS_TYPES


@dataclasses.dataclass
class Attribute:
    def __init__(self, name="Placeholder Name", category="Defense", power="0d0", dice_factor="TRUE", dice_advantage="0",
                 damage_types=[], aversions=[], incompats=[]):
        self.name = name
        self.category = category
        if power != "-":
            self.power = power
        else:
            self.power = "0"
        self.dice_factor = dice_factor
        if dice_advantage != "-":
            self.dice_advantage = dice_advantage
        else:
            self.dice_advantage = "0"
        self.damage_types = damage_types

        self.aversions = aversions

        self.incompats = incompats

    def get_damage_mods(self, damage):

        # modifier. + if bonus, - if defense
        modifier = "+"
        if self.category == "Defense":
            modifier = "-"

        # checks for any incompatibilities. if any exist, don't modify the damage.
        for a in self.incompats:
            if a in damage and a != "-":
                return ""
        # checks for any aversions. if any exist, double the penalty.
        base_penalty = 1
        for a in self.aversions:
            if a in damage and a != "-":
                base_penalty *= 2

        # builds actual damage string. put into a parenthesis to avoid formatting issues
        dmgstring = ""
        for t in self.damage_types:
            if t in damage and t != "-":
                net_power = self.power
                if self.dice_factor == "NONE":
                    div = net_power.split("+")
                    net_power = ""
                    for d in div:
                        if "d" not in d:
                            net_power += "+" + d
                elif self.dice_factor == "AVERAGE":
                    div = net_power.split("+")
                    net_power = ""
                    for d in div:
                        if "d" not in d:
                            net_power += "+" + d
                        else:
                            factors = d.split("d")
                            mult = factors[0]
                            by = int(int(factors[2]) / 2 + 1)
                            net_power += "+" + str(mult * by)
                net_power = "(" + net_power + ")"
                if int(self.dice_advantage) > 0:
                    net_power = "(" + self.dice_advantage + net_power + "kh1)"
                elif int(self.dice_advantage) < 0:
                    net_power = "(" + str(int(self.dice_advantage) * -1) + net_power + "kl1)"
                if base_penalty != 1:
                    net_power = net_power + "/" + str(base_penalty)
                net_power += " [" + t + "] "
                dmgstring += modifier + net_power
        return self.clean_damage_string(dmgstring)

    @classmethod
    def clean_damage_string(cls, dmgstring: str):
        dmg = dmgstring
        dmg = dmg.replace("+0", "")
        dmg = dmg.replace("(0+", "(")
        dmg = dmg.replace("(+", "(")
        if "+(0) [" in dmg:
            dmg = ""
        elif "-(0) [" in dmg:
            dmg = ""
        return dmg

    def to_dict(self):
        return {self.name: [self.category, self.power, self.dice_factor, self.dice_advantage, self.damage_types,
                            self.aversions, self.incompats]}

    @classmethod
    def from_dict(cls, d):
        return cls(**d)


class Attributes(HasIntegrationMixin):
    def __init__(self, attribute_list: list = []):
        super().__init__()
        self.attributes = attribute_list

    def copy(self):
        clone = Attributes([])
        for a in self.attributes:
            # raise Exception(a)
            clone.attributes.append(Attribute(
                a.name,
                a.category,
                a.power,
                a.dice_factor,
                a.dice_advantage,
                a.damage_types,
                a.aversions,
                a.incompats))
        return clone

    def get_damage_with_bonus(self, damage):
        temp_damage = ""
        for bon in self.attributes:
            if bon.category == "Bonus":
                temp_damage += bon.get_damage_mods(damage)
        return damage + temp_damage

    def get_damage_with_defense(self, damage):
        temp_damage = ""
        for bon in self.attributes:
            if bon.category == "Defense":
                temp_damage += bon.get_damage_mods(damage)

        return damage + temp_damage

    @classmethod
    def from_dict(cls, d):
        # raise Exception (d)
        attribute_list = []
        for entry in d:
            for key, value in entry.items():
                # raise Exception (value)
                attribute_list.append(Attribute(key, *value))
        return cls(attribute_list)

    def to_dict(self):
        return [a.to_dict() for a in self.attributes]
