import pytest

from simstack.models.parameters import Resource, Parameters, SlurmParameters
from simstack.core.resources import allowed_resources


class TestResource:
    """Test suite for the Resource class."""

    @pytest.fixture(autouse=True)
    def setup_allowed_resources(self):
        """Setup allowed resources before each test and clean up after."""
        # Store original state
        original_resources = allowed_resources.get_resources()

        # Set test resources
        allowed_resources.set_resources(
            ["cpu_cluster", "gpu_cluster", "high_memory", "self"]
        )

        yield

        # Restore original state
        allowed_resources.set_resources(original_resources)

    @pytest.fixture
    def empty_allowed_resources(self):
        """Mock empty allowed resources."""
        original_resources = allowed_resources.get_resources()
        allowed_resources.clear_resources()

        yield

        # Restore original state
        allowed_resources.set_resources(original_resources)

    def test_resource_creation_with_valid_value(self):
        """Test creating Resource with a valid value."""

        resource = Resource(value="cpu_cluster")
        assert resource.value == "cpu_cluster"
        assert str(resource) == "cpu_cluster"
        assert repr(resource) == "Resource(value='cpu_cluster')"

    def test_resource_creation_with_invalid_value(self):
        """Test creating Resource with invalid value raises ValidationError."""

        resource = Resource(value="invalid_resource")
        with pytest.raises(ValueError) as exc_info:
            resource.value

        msg = str(exc_info.value)
        assert "Invalid resource value" in msg
        assert "invalid_resource" in msg

    def test_resource_with_empty_allowed_values(self, empty_allowed_resources):
        """Test Resource accepts any value when no allowed values are configured."""

        resource = Resource(value="invalid_resource")
        with pytest.raises(ValueError) as exc_info:
            resource.value

        msg = str(exc_info.value)
        assert "Invalid resource value" in msg
        assert "invalid_resource" in msg

    def test_resource_allowed_values_property(self):
        """Test the allowed_values property."""

        Resource(value="cpu_cluster")
        expected_values = ["cpu_cluster", "gpu_cluster", "high_memory", "self"]
        assert Resource.allowed_values() == expected_values

    def test_resource_equality_with_resource_object(self):
        """Test Resource equality with another Resource object."""

        resource1 = Resource(value="cpu_cluster")
        resource2 = Resource(value="cpu_cluster")
        resource3 = Resource(value="gpu_cluster")

        assert resource1 == resource2
        assert resource1 != resource3

    def test_resource_equality_with_string(self):
        """Test Resource equality with string."""

        resource = Resource(value="cpu_cluster")

        assert resource == "cpu_cluster"
        assert resource != "gpu_cluster"

    def test_resource_equality_with_other_types(self):
        """Test Resource equality with other types."""

        resource = Resource(value="cpu_cluster")

        assert resource != 123
        assert resource is not None
        assert resource != ["cpu_cluster"]


class TestParameters:
    """Test suite for the Parameters class."""

    @pytest.fixture(autouse=True)
    def setup_allowed_resources(self):
        """Setup allowed resources before each test and clean up after."""
        # Store original state
        original_resources = allowed_resources.get_resources()

        # Set test resources
        allowed_resources.set_resources(
            ["cpu_cluster", "gpu_cluster", "high_memory", "self"]
        )

        yield

        # Restore original state
        allowed_resources.set_resources(original_resources)

    def test_parameters_creation_with_defaults(self):
        """Test creating Parameters with default values."""

        params = Parameters()

        assert params.force_rerun is False
        assert params.resource == "self"
        assert params.queue == "default"
        assert params.other_value == "other"
        assert params.test_dict == {"test": "value"}
        assert params.slurm_parameters == SlurmParameters()

    def test_parameters_creation_with_valid_resource(self):
        """Test creating Parameters with valid resource."""

        params = Parameters(
            force_rerun=True,
            resource=Resource(value="gpu_cluster"),
            queue="slurm-queue",
        )

        assert params.force_rerun is True
        assert params.resource == "gpu_cluster"
        assert params.queue == "slurm-queue"

    def test_parameters_creation_with_invalid_resource(self):
        """Test creating Parameters with invalid resource raises ValidationError."""

        resource = Resource(value="invalid_resource")
        with pytest.raises(ValueError) as exc_info:
            resource.value

        msg = str(exc_info.value)
        assert "Invalid resource value" in msg
        assert "invalid_resource" in msg

    def test_parameters_slurm_parameters_property_default(self):
        """Test slurm_parameters property with default values."""

        params = Parameters()
        slurm_params = params.slurm_parameters

        assert isinstance(slurm_params, SlurmParameters)
        assert slurm_params.nodes == 1
        assert slurm_params.job_name == "simstack"
        assert slurm_params.time == "1:00:00"

    def test_parameters_slurm_parameters_property_with_data(self):
        """Test slurm_parameters property with existing data."""

        slurm_data = {
            "nodes": 4,
            "job_name": "test_job",
            "time": "12:00:00",
            "partition": "gpu",
        }

        slurm_params = SlurmParameters(**slurm_data)
        params = Parameters(slurm_parameters=slurm_params)
        slurm_params = params.slurm_parameters

        assert slurm_params.nodes == 4
        assert slurm_params.job_name == "test_job"
        assert slurm_params.time == "12:00:00"
        assert slurm_params.partition == "gpu"

    def test_parameters_slurm_parameters_setter(self):
        """Test slurm_parameters setter."""

        params = Parameters()
        new_slurm = SlurmParameters(nodes=8, job_name="custom_job", time="24:00:00")

        params.slurm_parameters = new_slurm

        assert params.slurm_parameters.nodes == 8
        assert params.slurm_parameters.job_name == "custom_job"
        assert params.slurm_parameters.time == "24:00:00"

    def test_parameters_slurm_parameters_setter_none(self):
        """Test slurm_parameters setter with None."""

        params = Parameters(slurm_parameters_data={"nodes": 4})
        params.slurm_parameters = SlurmParameters()

        assert params.slurm_parameters == SlurmParameters()

    def test_parameters_full_model_validation(self):
        """Test full Parameters model validation with all fields."""

        test_data = {
            "force_rerun": True,
            "resource": Resource(value="high_memory"),
            "queue": "priority",
            "other_value": "custom",
            "test_dict": {"custom": "data", "number": 42},
            "slurm_parameters_data": {
                "nodes": 10,
                "job_name": "full_test",
                "time": "48:00:00",
                "partition": "special",
                "gres": "gpu:4",
            },
        }

        params = Parameters(**test_data)

        assert params.force_rerun is True
        assert params.resource == "high_memory"
        assert params.queue == "priority"
        assert params.other_value == "custom"
        assert params.test_dict == {"custom": "data", "number": 42}

        slurm_params = params.slurm_parameters
        assert slurm_params.nodes == 10
        assert slurm_params.job_name == "full_test"
        assert slurm_params.time == "48:00:00"
        assert slurm_params.partition == "special"
        assert slurm_params.gres == "gpu:4"


class TestIntegration:
    """Integration tests for Resource and Parameters classes."""

    @pytest.fixture(autouse=True)
    def setup_allowed_resources(self):
        """Setup allowed resources for integration testing."""
        # Store original state
        original_resources = allowed_resources.get_resources()

        # Set test resources
        allowed_resources.set_resources(["local", "cluster_a", "cluster_b", "gpu_node"])

        yield

        # Restore original state
        allowed_resources.set_resources(original_resources)

    def test_resource_parameters_integration(self):
        """Test Resource and Parameters working together."""

        # Create Resource directly
        resource = Resource(value="cluster_a")
        assert resource.value == "cluster_a"

        # Create Parameters with same resource value
        params = Parameters(resource=Resource(value="cluster_a"))
        assert params.resource == "cluster_a"

        # Verify they're equal
        assert resource == params.resource

    def test_parameters_model_dump_and_load(self):
        """Test serialization and deserialization of Parameters."""

        original_params = Parameters(
            force_rerun=True, resource=Resource(value="gpu_node"), queue="gpu_queue"
        )
        original_params.slurm_parameters = SlurmParameters(nodes=4, time="12:00:00")

        # Serialize
        params_dict = original_params.model_dump()

        # Deserialize
        restored_params = Parameters(**params_dict)

        assert restored_params.force_rerun == original_params.force_rerun
        assert restored_params.resource == original_params.resource
        assert restored_params.queue == original_params.queue
        assert (
            restored_params.slurm_parameters.nodes
            == original_params.slurm_parameters.nodes
        )
        assert (
            restored_params.slurm_parameters.time
            == original_params.slurm_parameters.time
        )


class TestAllowedResourcesIntegration:
    """Test integration with the AllowedResources singleton."""

    def test_dynamic_resource_changes(self):
        """Test that resources can be dynamically added and removed."""
        # Store original state
        original_resources = allowed_resources.get_resources()

        try:
            # Add some resources
            allowed_resources.set_resources(["resource1", "resource2"])

            # Should now validate against the new list
            valid_resource = Resource(value="resource1")
            assert valid_resource.value == "resource1"

            resource = Resource(value="invalid_resource")
            # Should reject invalid values
            with pytest.raises(ValueError):
                resource.value

        finally:
            # Restore original state
            allowed_resources.set_resources(original_resources)

    def test_allowed_resources_singleton_behavior(self):
        """Test that AllowedResources behaves as a singleton."""
        from simstack.core.resources import AllowedResources

        # Create two instances
        instance1 = AllowedResources()
        instance2 = AllowedResources()

        # They should be the same object
        assert instance1 is instance2

        # Changes to one should affect the other
        original_resources = instance1.get_resources()
        try:
            instance1.set_resources(["test1", "test2"])
            assert instance2.get_resources() == ["test1", "test2"]
        finally:
            instance1.set_resources(original_resources)


if __name__ == "__main__":
    pytest.main([__file__])
