"""Validated, bounded user-defined costs shared by receipts and contracts."""

from rest_framework import serializers


class NamedCostSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=80)
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0)


class CourierFeeSerializer(NamedCostSerializer):
    kind = serializers.ChoiceField(choices=["FIXED", "PERCENT"], default="FIXED")

    def validate(self, data):
        if data["kind"] == "PERCENT" and data["amount"] > 100:
            raise serializers.ValidationError("Percentage must be between 0 and 100.")
        return data


def json_costs(costs):
    return [{**cost, "amount": str(cost["amount"])} for cost in costs]
