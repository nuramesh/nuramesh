"""Data residency (ND45): a global deployment accepts only synthetic data."""

import pytest

from nuramesh.core.residency import ResidencyError, check_processing, require_data_residency


def test_global_accepts_only_synthetic_data() -> None:
    require_data_residency("global", "synthetic")
    with pytest.raises(ResidencyError, match="outside Canada"):
        require_data_residency("global", "customer")
    with pytest.raises(ResidencyError):  # a customer-chosen region list never rescues "global"
        require_data_residency("global", "customer", customer_regions=("global", "canadaeast"))


def test_canada_accepts_any_data_class() -> None:
    require_data_residency("canada", "synthetic")
    require_data_residency("canada", "customer")


def test_a_region_accepts_customer_data_only_if_the_customer_chose_it() -> None:
    require_data_residency("region:canadaeast", "synthetic")
    require_data_residency("region:canadaeast", "customer", customer_regions=("canadaeast",))
    with pytest.raises(ResidencyError, match="chosen regions"):
        require_data_residency("region:eastus", "customer", customer_regions=("canadaeast",))
    with pytest.raises(ResidencyError):
        require_data_residency("region:eastus", "customer")


@pytest.mark.parametrize("bad", ["", "Global", "region:", "region:Bad Region", "europe", "global "])
def test_unknown_scopes_and_data_classes_are_refused(bad: str) -> None:
    with pytest.raises(ResidencyError):
        check_processing(bad)
    with pytest.raises(ResidencyError):
        require_data_residency(bad, "synthetic")


def test_an_unknown_data_class_is_refused_even_for_canada() -> None:
    for data_class in ("", "Customer", "pii", "none"):
        with pytest.raises(ResidencyError):
            require_data_residency("canada", data_class)
